"""
UI components for ZEN API streaming visualization.

This module provides the StreamingViewer class which manages the Napari viewer
for displaying streaming microscopy data from ZEN Blue.

Architecture:
- During streaming: A 2D latest-frame layer by default, or an optional 7D
    full-history layer (N, 1, 1, 1, 1, Y, X)
- After completion: Multiple 5D STZYX scene-mosaic layers, one per channel

Two acquisition modes:
1. Napari-started: User starts from UI, has experiment_id for status monitoring
2. ZEN-started: User starts from ZEN, uses timeout for completion detection

Thread Safety:
- Uses Qt signals for thread-safe communication between worker and main thread
- All napari/Qt operations happen in main thread via signal slots
- Async operations (ZEN API calls) happen in worker thread event loop
"""

import asyncio
import contextlib
import itertools
import logging
import time
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import napari
import numpy as np
from napari import Viewer
from napari.layers import Image
from qtpy.QtCore import QObject, QTimer, Signal
from qtpy.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from napari_zen_streaming._scene_geometry import (
    TileGeometry,
    assemble_channel_scene_mosaics,
)
from napari_zen_streaming.ZEN_config import ImageMetadata, ZENConfig
from napari_zen_streaming.ZEN_init import ZENConnection

logger = logging.getLogger(__name__)

# Seconds after restructure during which incoming frames are treated
# as stragglers (belonging to the finished experiment) and silently
# dropped.  After this grace period, new frames are accepted as a
# new ZEN-started acquisition.
_STRAGGLER_GRACE_SECONDS: float = 3.0
_PROGRESS_UPDATE_INTERVAL_SECONDS: float = 0.1
_PROGRESS_DESCRIPTIONS = (
    "Streaming frames",
    "Writing OME-ZARR",
    "OME-ZARR saved",
)


def _resolve_streamed_z_spacing_um(
    metadata_by_key: dict[tuple[int, int, int, int, int], ImageMetadata],
    fallback_um: float,
) -> float:
    """Resolve Z spacing from streamed stage positions when available.

    Stage positions are compared only within one scene, time point, tile, and
    channel so absolute offsets between independent stacks cannot distort the
    result. The XML-derived value remains the fallback for single-plane data
    or streams without usable stage positions.
    """
    stacks: dict[tuple[int, int, int, int], list[tuple[int, float]]] = {}
    for (
        scene,
        time_index,
        tile,
        z_index,
        channel,
    ), metadata in metadata_by_key.items():
        stage_z_um = metadata.stage_z_um
        if not np.isfinite(stage_z_um):
            continue
        stack_key = (scene, time_index, tile, channel)
        stacks.setdefault(stack_key, []).append((z_index, stage_z_um))

    spacing_candidates: list[float] = []
    for stack in stacks.values():
        ordered_planes = sorted(stack)
        for (first_index, first_z), (
            second_index,
            second_z,
        ) in itertools.pairwise(ordered_planes):
            index_delta = second_index - first_index
            if index_delta <= 0:
                continue
            spacing = abs(second_z - first_z) / index_delta
            if np.isfinite(spacing) and spacing > 0:
                spacing_candidates.append(spacing)

    if not spacing_candidates:
        return fallback_um
    return float(np.median(spacing_candidates))


class _ThrottledProgressEmitter:
    """Coalesce high-rate progress updates before they enter Qt's queue.

    Pixel reception and OME-ZARR writes remain unthrottled. Only visual
    progress reporting is rate-limited, while start, first-frame, final-frame,
    and completion events are always delivered.
    """

    def __init__(
        self,
        emit: Callable[[int, int], None],
        interval: float = _PROGRESS_UPDATE_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the progress emitter.

        Args:
            emit: Callback receiving ``(current, total)`` progress values.
            interval: Minimum seconds between ordinary visual updates.
            clock: Monotonic time provider, injectable for tests.
        """
        self._emit = emit
        self._interval = interval
        self._clock = clock
        self._last_emitted_at = float("-inf")

    def __call__(self, current: int, total: int) -> None:
        """Emit boundary events immediately and coalesce intermediate ones."""
        now = self._clock()
        is_boundary = current < 0 or current <= 1 or (total > 0 and current >= total)
        if not is_boundary and now - self._last_emitted_at < self._interval:
            return
        self._emit(current, total)
        self._last_emitted_at = now


class ViewerSignals(QObject):
    """
    Qt signals for thread-safe viewer updates.
    These signals allow the worker thread (running async ZEN API operations)
    to safely communicate with the main Qt thread (which owns the napari viewer).

    Signals:
    - create_layer: Signal to create initial streaming layer
    - update_layer: Signal to update streaming layer with new data
    - restructure_complete: Signal to split streaming layer into channel layers
    - experiment_finished: Signal that experiment has completed
    - set_live_preview_option_enabled: Signal to lock or unlock the checkbox
    """

    create_layer = Signal(object, object)  # image, metadata
    update_layer = Signal(object, object)  # data_array, metadata_list
    restructure_complete = Signal(object)  # channel_data dict
    experiment_finished = Signal()
    clear_all_layers = Signal()
    open_omezarr = Signal(str)  # zarr_path

    show_stop_button = Signal(bool)
    show_start_button = Signal(bool)
    update_progress = Signal(int, int)  # current, total
    experiment_metadata_loaded = Signal(object)
    set_live_preview_option_enabled = Signal(bool)


class StreamingViewer:
    """
    Manages Napari viewer for streaming microscopy data.

    Architecture:
        - During streaming: A 2D latest-frame layer by default, or an optional
            7D full-history layer (N, 1, 1, 1, 1, Y, X)
    - After completion: Multiple 5D STZYX scene-mosaic layers, one per channel

    Two acquisition modes:
    1. Napari-started: User starts from UI, has experiment_id for status monitoring
    2. ZEN-started: User starts from ZEN, uses timeout for completion detection

    Thread Safety:
    - All napari/Qt operations must occur in the main thread
    - Worker thread communicates via Qt signals
    - Uses QTimer for safe cross-thread Qt operations
    """

    def __init__(self, config: ZENConfig):
        """
        Initialize streaming viewer.

        Args:
            config: ZEN configuration object containing display settings,
                   buffer sizes, and acquisition mode

        Note:
            Creates or reuses existing napari viewer and sets up UI components.
            The viewer must be initialized in the Qt main thread.
        """
        self.config = config

        # Initialize Napari viewer
        _viewer = napari.current_viewer()
        if _viewer is None:
            logger.debug("No existing Napari viewer found, creating new one...")
            _viewer = Viewer()
        self.viewer: Viewer = _viewer

        # Set externally by main application before streaming starts
        self.connection: ZENConnection | None = None
        # Set externally by main application (ZENApplication instance)
        self.app: Any = None

        # Effective channel index for the gRPC pixel stream.
        # Initialised from the config (.env / CLI) and overridable
        # via the Channel filter combo box at runtime.  The pipeline
        # reads this value each time it opens a new reader.
        self._effective_channel_index: int | None = config.channel_index

        # Qt signals for thread-safe updates
        # These allow worker thread to safely communicate with main Qt thread
        self.signals = ViewerSignals()
        self.signals.create_layer.connect(self._create_layer_slot)
        self.signals.update_layer.connect(self._update_layer_slot)
        self.signals.restructure_complete.connect(self._restructure_complete_slot)
        self.signals.experiment_finished.connect(self._experiment_finished_slot)
        self.signals.clear_all_layers.connect(self._clear_layers)
        self.signals.open_omezarr.connect(self._open_omezarr_slot)
        self.signals.experiment_metadata_loaded.connect(self._apply_experiment_metadata)

        # ========== Layer Management ==========
        # Single streaming layer during acquisition (7D: STMZCYX)
        self.image_layer: Image | None = None

        # Multiple channel layers after restructure (5D: STZYX per channel)
        self.image_layers: dict[int, Image] = {}

        # ========== Frame Data Storage ==========
        # Sparse storage: Key = (s, t, m, z, c), Value = image array (Y, X)
        # This allows us to store only acquired frames without pre-allocating full array
        self.frame_data: dict[tuple[int, int, int, int, int], np.ndarray] = {}
        self.frame_metadata_by_key: dict[tuple[int, int, int, int, int], ImageMetadata] = {}

        # Ordered list of all frame keys for reconstructing arrays
        self.frame_keys: list[tuple[int, int, int, int, int]] = []

        # Track unique values for dimension sizes
        self.image_shape: tuple[int, int] | None = None
        self.unique_channels: set[int] = set()

        # ========== Metadata Storage ==========
        # Pixel scaling in micrometers (converted from meters by utils)
        self.scaling_y_um: float = 1.0
        self.scaling_x_um: float = 1.0
        # Z spacing (µm) loaded from experiment XML when available.
        self._z_spacing_um: float = 1.0

        # ========== Streaming State ==========
        # True during acquisition, False after completion
        self.is_streaming: bool = True
        # The checkbox controls the next acquisition. Its value is copied
        # into this run-specific field before worker-thread processing starts.
        self._live_latest_preference: bool = getattr(
            config,
            "show_only_last_frame_during_live",
            True,
        )
        self._run_live_latest: bool | None = None

        # Path of last written OME-ZARR (set by _write_omezarr)
        self._last_zarr_path: Path | None = None

        # ========== On-the-fly OME-ZARR write state ==========
        # Active ome-writers stream context (opened on first frame in
        # OME-ZARR mode, closed in _finalize_zarr_stream).
        self._zarr_stream: Any = None
        # (t, s, m, c, z) → linear_index mapping for the current run.
        self._zarr_coord_to_linear: dict[tuple[int, ...], int] = {}
        # Out-of-order frame buffer: linear_idx → (frame_array, meta_dict)
        self._zarr_pending: dict[int, tuple[np.ndarray, dict]] = {}
        # Next consecutive linear index to flush to disk.
        self._zarr_next_write: int = 0
        # Total frames expected from config (drives progress reporting).
        self._zarr_total_expected: int = 0
        # Output path of the zarr currently being written.
        self._zarr_path: Path | None = None
        # Count of frames received in the current zarr session.
        self._zarr_frame_count: int = 0
        # Plugin ExperimentConfig created from UI and application defaults.
        self._zarr_ecfg: Any = None  # ExperimentConfig | None
        # When True the pipeline should pass raw pixel data (no
        # normalisation) so that the zarr writer stores original values.
        # Set from the main thread in on_start_clicked / add_frame;
        # read from the worker thread in the pipeline.
        self._zarr_write_mode: bool = False
        # Guard: True while finalize_zarr_acquisition is running
        # or has already completed for the current session.
        self._zarr_finalizing: bool = False
        # Guard: True while the standalone OME-ZARR writer
        # (stream_to_omezarr_with_config) is running.  While set,
        # the pipeline-based add_frame path discards frames so
        # that only the standalone writer (which has its own
        # dedicated gRPC channel) consumes pixel data.
        self._standalone_zarr_active: bool = False

        # Timestamp of last received frame (for timeout detection in ZEN-started mode)
        self.last_frame_time: float = 0.0

        # Timestamp when acquisition finalization completed
        # (for straggler detection)
        self._restructure_completed_at: float = 0.0

        # Task for detecting when to finalize the acquisition
        self.restructure_task: asyncio.Task | None = None

        # Task for monitoring experiment status (napari-started only)
        self.status_monitor_task: asyncio.Task | None = None

        # Experiment ID (napari-started only, None for ZEN-started)
        self.current_experiment_id: str | None = None

        # ========== Buffering for Performance ==========
        # Buffer incoming frames to reduce UI update frequency
        # This improves performance by batching updates instead of updating on every frame
        self.image_buffer: deque = deque(maxlen=config.image_buffer_size)
        self.metadata_buffer: deque = deque(maxlen=config.image_buffer_size)
        self.processing_lock = asyncio.Lock()
        self.is_processing = False

        # ========== UI Components (if UI mode enabled) ==========
        self.dropdown_widget = QWidget()
        self.dropdown_layout = QVBoxLayout(self.dropdown_widget)

        # Title label
        label = QLabel("Available ZEN experiment setups:")
        label.setStyleSheet("font-weight: bold; margin-bottom: 5px;")

        # Experiment selector dropdown
        self.dropdown = QComboBox()
        self.dropdown.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.dropdown.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.dropdown.currentTextChanged.connect(self._on_experiment_changed)

        # Warning label about layer removal
        warning_label = QLabel("Starting a new Experiment from Napari or ZEN will remove existing layers!")
        warning_label.setStyleSheet("color: #FF6B35; margin-top: 5px; margin-bottom: 5px;")
        warning_label.setWordWrap(True)

        # Start button
        button_text = "Start Selected Experiment" if self.config.exp_started_by_ui else "Experiment Running..."
        self.start_button = QPushButton(button_text)
        self.start_button.setStyleSheet("background-color: #0040A6; color: white; border-radius: 6px; padding: 4px;")
        self.start_button.clicked.connect(self.on_start_clicked)

        self.stop_button = QPushButton("Stop Running Experiment")
        self.stop_button.setStyleSheet("background-color: #A60000; color: white; border-radius: 6px; padding: 4px;")

        self.stop_button.clicked.connect(self.on_stop_clicked)
        self.stop_button.setVisible(False)

        self.signals.show_stop_button.connect(self.stop_button.setVisible)
        self.signals.show_start_button.connect(self._set_start_button_enabled)

        # ========== Stream Mode ==========
        mode_label = QLabel("Stream mode:")
        mode_label.setStyleSheet("font-weight: bold; margin-top: 10px; margin-bottom: 2px;")
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(
            [
                "Display only",
                "OME-ZARR only",
            ]
        )

        # Channel filter row (Display only mode)
        self.channel_filter_row = QWidget()
        ch_layout = QHBoxLayout(self.channel_filter_row)
        ch_layout.setContentsMargins(0, 0, 0, 0)
        ch_label = QLabel("Channel filter:")
        self.channel_filter_combo = QComboBox()
        self.channel_filter_combo.setEditable(False)
        self.channel_filter_combo.addItem("All channels", None)
        for i in range(32):
            self.channel_filter_combo.addItem(str(i), i)
        # Pre-select from config
        if self.config.channel_index is not None:
            idx = self.channel_filter_combo.findData(self.config.channel_index)
            if idx >= 0:
                self.channel_filter_combo.setCurrentIndex(idx)
        self.channel_filter_combo.setToolTip(
            "Filter the pixel stream to a single channel\n"
            "index, or receive all channels.\n"
            "Takes effect on the next experiment start."
        )
        self.channel_filter_combo.currentIndexChanged.connect(self._on_channel_filter_changed)
        ch_layout.addWidget(ch_label)
        ch_layout.addWidget(self.channel_filter_combo)

        self.chk_live_latest = QCheckBox("Show only last frame during live")
        self.chk_live_latest.setChecked(self._live_latest_preference)
        self.chk_live_latest.setToolTip(
            "Show a lightweight 2D preview while retaining every frame "
            "for final scene assembly. Uncheck to display the complete "
            "frame history during acquisition."
        )
        self.chk_live_latest.toggled.connect(self._set_live_latest_preference)
        self.signals.set_live_preview_option_enabled.connect(self.chk_live_latest.setEnabled)

        # Output directory row
        self.omezarr_dir_row = QWidget()
        dir_layout = QHBoxLayout(self.omezarr_dir_row)
        dir_layout.setContentsMargins(0, 0, 0, 0)
        self.omezarr_dir_edit = QLineEdit(
            self.config.default_output_dir if self.config.default_output_dir else str(Path.home() / "Documents")
        )
        self.omezarr_dir_edit.setPlaceholderText("Output directory")
        browse_button = QPushButton("...")
        browse_button.setFixedWidth(30)
        browse_button.clicked.connect(self._browse_omezarr_dir)
        dir_layout.addWidget(self.omezarr_dir_edit)
        dir_layout.addWidget(browse_button)

        # Compression selector
        self.omezarr_comp_row = QWidget()
        comp_layout = QHBoxLayout(self.omezarr_comp_row)
        comp_layout.setContentsMargins(0, 0, 0, 0)
        comp_label = QLabel("Compression:")
        self.compression_combo = QComboBox()
        self.compression_combo.addItems(["blosc-zstd", "blosc-lz4", "zstd", "none"])
        comp_layout.addWidget(comp_label)
        comp_layout.addWidget(self.compression_combo)

        # Progress bar for OME-ZARR writing — uses napari's activity bar
        # (napari.utils.progress instance stored in self._napari_progress)

        # Dimensions panel populated from selected experiment metadata.
        from qtpy.QtWidgets import (
            QDoubleSpinBox,
            QFormLayout,
            QSpinBox,
        )

        self.dim_panel = QWidget()
        dim_form = QFormLayout(self.dim_panel)
        dim_form.setContentsMargins(0, 0, 0, 0)
        dim_form.setSpacing(4)

        # T, C, and Z are derived from the selected ZEN experiment.  Keep
        # spinboxes for compact numeric display and shared total-frame logic,
        # but prevent manual dimension overrides.
        self.spin_t = QSpinBox()
        self.spin_t.setRange(1, 100_000)
        self.spin_t.setValue(1)
        self.spin_c = QSpinBox()
        self.spin_c.setRange(1, 32)
        self.spin_c.setValue(1)
        self.spin_z = QSpinBox()
        self.spin_z.setRange(1, 10_000)
        self.spin_z.setValue(1)
        for spinbox in (self.spin_t, self.spin_c, self.spin_z):
            spinbox.setReadOnly(True)
            spinbox.setButtonSymbols(QSpinBox.NoButtons)
            spinbox.setEnabled(False)
        self.spin_z_spacing = QDoubleSpinBox()
        self.spin_z_spacing.setRange(0.001, 10_000.0)
        self.spin_z_spacing.setDecimals(2)
        self.spin_z_spacing.setSingleStep(0.1)
        self.spin_z_spacing.setValue(1.0)
        self.spin_z_spacing.setReadOnly(True)
        self.spin_z_spacing.setButtonSymbols(QDoubleSpinBox.NoButtons)
        self.spin_z_spacing.setEnabled(False)

        # Tiles and scenes are derived from the selected ZEN experiment.
        # Use the same disabled numeric presentation as T, C, and Z.
        self.label_tiles = QSpinBox()
        self.label_tiles.setRange(1, 1_000_000)
        self.label_scenes = QSpinBox()
        self.label_scenes.setRange(1, 1_000_000)
        for spinbox in (self.label_tiles, self.label_scenes):
            spinbox.setReadOnly(True)
            spinbox.setButtonSymbols(QSpinBox.NoButtons)
            spinbox.setEnabled(False)
        self.label_total = QLabel("1")
        self.label_total.setStyleSheet("font-weight: bold;")

        # Whether the plugin calls the ZEN API to trigger the experiment
        # or waits for the user to start it manually in ZEN Blue.
        self.chk_auto_trigger = QCheckBox("Start experiment via ZEN API")
        self.chk_auto_trigger.setChecked(True)
        self.chk_auto_trigger.setToolTip(
            "Checked: clicking Start will trigger the selected\n"
            "experiment via the ZEN API automatically.\n"
            "Unchecked: click Start to arm the writer, then\n"
            "start the experiment manually in ZEN Blue."
        )
        # Update the Start button label whenever the checkbox changes
        self.chk_auto_trigger.toggled.connect(self._on_auto_trigger_changed)

        self.chk_hcs_layout = QCheckBox("Write HCS plate layout")
        self.chk_hcs_layout.setChecked(True)
        self.chk_hcs_layout.setToolTip(
            "Write wells and fields using the OME-ZARR HCS plate layout.\n"
            "Available only when every scene has explicit ZEN well metadata."
        )
        self.chk_hcs_layout.setVisible(False)

        self.chk_keep_source_tiles = QCheckBox("Keep individual source tiles")
        self.chk_keep_source_tiles.setChecked(False)
        self.chk_keep_source_tiles.setToolTip(
            "Keep the individual tile groups after generic scene mosaics "
            "are assembled. Unchecked removes them to avoid duplicated "
            "pixel storage. HCS position groups are always retained."
        )
        self.chk_hcs_layout.toggled.connect(self._update_source_tile_option)

        self.chk_create_pyramid = QCheckBox("Create pyramid after acquisition")
        self.chk_create_pyramid.setChecked(False)
        self.chk_create_pyramid.setToolTip(
            "Generate downsampled OME-Zarr levels after all streamed pixels "
            "have been written. Napari opens the result after generation."
        )

        self.spin_pyramid_levels = QSpinBox()
        self.spin_pyramid_levels.setRange(1, 6)
        self.spin_pyramid_levels.setValue(3)
        self.spin_pyramid_levels.setEnabled(False)
        self.spin_pyramid_levels.setToolTip("Number of 2x Y/X downsampled levels to append.")
        self.chk_create_pyramid.toggled.connect(self.spin_pyramid_levels.setEnabled)

        dim_form.addRow("Time points (T):", self.spin_t)
        dim_form.addRow("Channels (C):", self.spin_c)
        dim_form.addRow("Z planes (Z):", self.spin_z)
        dim_form.addRow("Z spacing (µm, XML):", self.spin_z_spacing)
        dim_form.addRow("Tiles (M):", self.label_tiles)
        dim_form.addRow("Scenes (S):", self.label_scenes)
        dim_form.addRow("Total frames:", self.label_total)
        dim_form.addRow(self.chk_hcs_layout)
        dim_form.addRow(self.chk_keep_source_tiles)
        dim_form.addRow(self.chk_create_pyramid)
        dim_form.addRow("Pyramid levels:", self.spin_pyramid_levels)

        self.dim_panel.setVisible(False)

        # Recalculate total when metadata updates the dimensions.
        # Keep _z_spacing_um in sync so the restructure slot uses the
        # XML-derived value when metadata is available.
        self.spin_z_spacing.valueChanged.connect(self._on_z_spacing_changed)

        # The auto-trigger checkbox sits directly below the
        # Start/Stop buttons so it is easy to reach.
        # It is only shown when OME-ZARR mode is active.
        self.auto_trigger_row = self.chk_auto_trigger

        # Add widgets to layout
        self.dropdown_layout.addWidget(label)
        self.dropdown_layout.addWidget(self.dropdown)
        self.dropdown_layout.addWidget(warning_label)
        self.dropdown_layout.addWidget(self.start_button)
        self.dropdown_layout.addWidget(self.stop_button)
        self.dropdown_layout.addWidget(self.auto_trigger_row)
        self.dropdown_layout.addWidget(mode_label)
        self.dropdown_layout.addWidget(self.mode_combo)
        self.dropdown_layout.addWidget(self.channel_filter_row)
        self.dropdown_layout.addWidget(self.chk_live_latest)
        self.dropdown_layout.addWidget(self.omezarr_dir_row)
        self.dropdown_layout.addWidget(self.omezarr_comp_row)
        self.dropdown_layout.addWidget(self.dim_panel)
        self.dropdown_layout.addStretch(1)

        # Hide OME-ZARR options initially ("Display only" is default)
        self.auto_trigger_row.setVisible(False)
        self.omezarr_dir_row.setVisible(False)
        self.omezarr_comp_row.setVisible(False)

        # Total expected frames from config (None = unknown)
        self._expected_total_frames: int | None = None
        # Metadata cached for the currently selected experiment.
        self._selected_experiment_metadata: Any = None
        self._metadata_request_id: int = 0
        # napari activity-bar progress instance (created per acquisition)
        self._napari_progress: Any = None
        self._napari_progress_bars: set[Any] = set()
        self._progress_phase = "idle"

        # Toggle OME-ZARR widget visibility on mode change
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.signals.update_progress.connect(self._update_progress_slot)

        # Add dock widget to napari window
        self.viewer.window.add_dock_widget(self.dropdown_widget, name="Experiment Selector", area="right")

        logger.debug("Napari viewer initialized")

    @property
    def display_enabled(self) -> bool:
        """True when the stream should update the napari viewer."""
        return self.mode_combo.currentIndex() == 0

    @property
    def omezarr_enabled(self) -> bool:
        """True when the stream should be saved to OME-ZARR."""
        return self.mode_combo.currentIndex() == 1

    def restructure_button_clicked(self):
        logger.warning("User requested immediate restructure via button")

    def _on_mode_changed(self, index: int) -> None:
        """Show/hide OME-ZARR widgets based on the selected mode.

        Args:
            index: Combo box index (0=Display only, 1=OME-ZARR only).
        """
        show_zarr = index == 1
        self.auto_trigger_row.setVisible(show_zarr)
        self.channel_filter_row.setVisible(not show_zarr)
        self.chk_live_latest.setVisible(not show_zarr)
        self.omezarr_dir_row.setVisible(show_zarr)
        self.omezarr_comp_row.setVisible(show_zarr)
        self.dim_panel.setVisible(show_zarr)
        self._update_hcs_layout_option()
        # Keep the start button label consistent with the new mode
        if self.start_button.isEnabled():
            self._sync_start_button_label()

    def _set_live_latest_preference(self, enabled: bool) -> None:
        """Store the latest-frame preference for the next acquisition.

        Args:
            enabled: Whether live Display mode should show only the newest
                frame.
        """
        self._live_latest_preference = enabled

    def _freeze_live_preview_option(self) -> None:
        """Freeze the live-preview behavior for the current acquisition.

        This method only reads plain Python state so it is safe when the
        first frame of a ZEN-started acquisition arrives on the worker loop.
        Widget changes are delegated to the Qt thread through a signal.
        """
        if self._run_live_latest is not None:
            return
        self._run_live_latest = self._live_latest_preference
        self.signals.set_live_preview_option_enabled.emit(False)

    def _release_live_preview_option(self) -> None:
        """Allow the preview option to be changed for the next run."""
        self._run_live_latest = None
        self.chk_live_latest.setEnabled(True)

    def _on_dim_changed(self) -> None:
        """Recalculate total frames when T, C or Z spinboxes change."""
        t = self.spin_t.value()
        c = self.spin_c.value()
        z = self.spin_z.value()
        m = self.label_tiles.value()
        s = self.label_scenes.value()
        total = t * c * z * m * s
        self.label_total.setText(str(total))
        self._expected_total_frames = total

    def _update_hcs_layout_option(self) -> None:
        """Show the HCS option when all scenes map to explicit wells."""
        metadata = getattr(self, "_selected_experiment_metadata", None)
        positions = getattr(metadata, "positions", ()) if metadata else ()
        scenes = getattr(metadata, "scenes", 0) if metadata else 0
        scene_indices = {position.get("scene_index") for position in positions if isinstance(position, dict)}
        complete = (
            scenes > 0
            and len(positions) == scenes
            and scene_indices == set(range(scenes))
            and all(
                isinstance(position.get("well_row"), int)
                and position["well_row"] > 0
                and isinstance(position.get("well_column"), int)
                and position["well_column"] > 0
                for position in positions
            )
        )
        self.chk_hcs_layout.setVisible(self.omezarr_enabled and complete)
        self.chk_hcs_layout.setEnabled(complete)
        if not complete:
            self.chk_hcs_layout.setChecked(False)
            self.chk_hcs_layout.setText("Write HCS plate layout")
            self._update_source_tile_option()
            return

        well_ids = list(
            dict.fromkeys(str(position.get("well_id")) for position in positions if position.get("well_id"))
        )
        suffix = f" ({', '.join(well_ids)})" if well_ids else ""
        self.chk_hcs_layout.setText(f"Write HCS plate layout{suffix}")
        self._update_source_tile_option()

    def _update_source_tile_option(
        self,
        _checked: bool | None = None,
    ) -> None:
        """Show source-tile retention only for generic scene mosaics."""
        checkbox = getattr(self, "chk_keep_source_tiles", None)
        if checkbox is None:
            return
        hcs_active = self.chk_hcs_layout.isVisible() and self.chk_hcs_layout.isChecked()
        checkbox.setVisible(self.omezarr_enabled and not hcs_active)
        checkbox.setEnabled(not hcs_active)

    def _on_z_spacing_changed(self, value: float) -> None:
        """Keep ``_z_spacing_um`` in sync with the displayed value.

        The value is normally supplied by experiment XML.  The signal
        also updates display-mode restructure state when the spinbox is
        not visible.

        Args:
            value: New Z spacing in µm.
        """
        self._z_spacing_um = value

    def _on_channel_filter_changed(self, index: int) -> None:
        """Update the effective channel index and restart the reader.

        Immediately restarts the pipeline's gRPC reader so the new
        ``channel_index`` takes effect without waiting for the user
        to click Start.  This is important for ZEN-started
        experiments where no Start click happens.

        Args:
            index: Combo box index (0 = all, 1..32 = specific).
        """
        value = self.channel_filter_combo.currentData()
        self._effective_channel_index = value
        if value is None:
            logger.info("Channel filter set to: all channels")
        else:
            logger.info(f"Channel filter set to: {value}")

        # Restart the pipeline reader so the new filter is active
        # immediately (not just on next Start click).
        pipeline = getattr(self.app, "pipeline", None)
        if pipeline is not None:

            async def _restart_reader() -> None:
                await pipeline.suspend_reader()
                await pipeline.resume_reader()

            self.safe_execution(_restart_reader)

    def _expected_display_frame_count(self) -> int:
        """Return the expected unique frame count for Display mode."""
        metadata = self._selected_experiment_metadata
        if metadata is None:
            return self._expected_total_frames or 0

        channels = 1 if self._effective_channel_index is not None else metadata.channels
        return metadata.time_points * channels * metadata.z_planes * metadata.tiles * metadata.scenes

    def _received_display_frame_keys(
        self,
    ) -> set[tuple[int, int, int, int, int]]:
        """Return stored and buffered unique Display-mode frame keys."""
        keys = set(self.frame_data)
        keys.update(
            (
                metadata.frame_s,
                metadata.frame_t,
                metadata.frame_m,
                metadata.frame_z,
                metadata.frame_c,
            )
            for metadata in self.metadata_buffer
        )
        return keys

    async def _wait_for_display_stream_completion(self) -> int:
        """Wait for trailing pixels after ZEN reports experiment finished.

        ZEN can publish its finished status before the gRPC pixel stream has
        delivered every frame. Prefer the expected unique-key count and use
        pixel-stream inactivity as the fallback for incomplete acquisitions.

        Returns:
            Number of unique frame keys received before finalization.
        """
        expected = self._expected_display_frame_count()
        status_finished_at = time.time()
        timeout = self.config.restructure_timeout

        while True:
            received = len(self._received_display_frame_keys())
            if expected > 0 and received >= expected:
                logger.info(
                    "Display stream complete after status finish: %d/%d frames",
                    received,
                    expected,
                )
                return received

            last_activity = self.last_frame_time or status_finished_at
            idle = time.time() - last_activity
            if idle >= timeout:
                logger.warning(
                    "Display stream inactive for %.1fs after status finish: "
                    "%d/%d expected frames; restructuring available data",
                    idle,
                    received,
                    expected,
                )
                return received

            await asyncio.sleep(min(0.1, timeout))

    def _sync_start_button_label(self) -> None:
        """Set the start button to its correct idle label.

        Call this whenever the button returns to the idle (enabled) state
        or when the mode / auto-trigger checkbox changes while idle.
        The label depends on both the stream mode and whether the
        plugin will trigger the experiment automatically.
        """
        if self.omezarr_enabled and not self.chk_auto_trigger.isChecked():
            self.start_button.setText("Enable OME-ZARR Writer (start Experiment manually in ZEN)")
        else:
            self.start_button.setText("Start Selected Experiment")

    def _on_auto_trigger_changed(self, checked: bool) -> None:
        """Update the Start button label when the auto-trigger checkbox changes.

        Args:
            checked: True if the plugin will trigger the experiment
                via the ZEN API; False if the user starts it manually
                in ZEN Blue.
        """
        # Only update idle label; leave running-state labels untouched.
        if self.start_button.isEnabled():
            self._sync_start_button_label()

    def _update_progress_slot(self, current: int, total: int) -> None:
        """Update napari's activity-bar progress bar from the main thread.

        Sentinel values for ``current``:
            -1 : writing phase started (close streaming bar, open write bar)
            -2 : writing complete (close the bar)

        Args:
            current: Frames received so far, or a negative sentinel.
            total: Total frames expected; 0 means indeterminate.
        """
        try:
            from napari.utils import progress as nap_progress
        except Exception:
            logger.warning("napari.utils.progress unavailable; skipping progress bar")
            return

        try:
            if current == -2:
                # Writing complete — close every bar owned by this viewer.
                self._close_progress_bar()
                return

            # First frame detected: update button text so the user
            # knows that both ZEN and the pixel stream are active.
            if (
                current > 0
                and not self.start_button.isEnabled()
                and self.start_button.text() != "Experiment & Stream Running"
            ):
                self.start_button.setText("Experiment & Stream Running")

            if current == -1:
                # Writing phase — close all streaming bars before opening one
                # determinate bar for the write loop.
                self._close_progress_bar()
                pbar_total = total if total > 0 else None
                self._napari_progress = nap_progress(total=pbar_total)
                self._napari_progress_bars.add(self._napari_progress)
                self._progress_phase = "writing"
                self._napari_progress.set_description("Writing OME-ZARR")
                return

            # --- streaming / writing progress ---
            if self._napari_progress is None:
                # The standalone writer starts with a -1 sentinel. Ignore
                # queued pipeline updates until that sentinel arrives so they
                # cannot create a second, stale "Streaming frames" bar.
                if (
                    getattr(self, "_standalone_zarr_active", False)
                    and getattr(self, "_progress_phase", "idle") != "writing"
                ):
                    return
                pbar_total = total if total > 0 else None
                self._napari_progress = nap_progress(total=pbar_total)
                self._napari_progress_bars.add(self._napari_progress)
                self._progress_phase = "streaming"
                self._napari_progress.set_description("Streaming frames")

            if total > 0:
                # Advance to absolute position (tqdm.update is relative)
                delta = current - self._napari_progress.n
                if delta > 0:
                    self._napari_progress.update(delta)
            else:
                # Indeterminate — just tick to show activity
                self._napari_progress.update(1)

        except Exception:
            logger.exception("Error updating napari progress bar")

    def _close_progress_bar(self) -> None:
        """Close all plugin-owned napari activity-bar progress bars.

        Safe to call at any time from the main thread.  No-op when
        no bar is open. This also removes matching orphan bars from an older
        viewer instance, which can remain after a plugin reload.
        """
        bars = set(getattr(self, "_napari_progress_bars", set()))
        current = getattr(self, "_napari_progress", None)
        if current is not None:
            bars.add(current)

        try:
            from napari.utils import progress as nap_progress

            bars.update(
                progress_bar
                for progress_bar in tuple(nap_progress._all_instances)
                if str(getattr(progress_bar, "desc", "")).startswith(_PROGRESS_DESCRIPTIONS)
            )
        except Exception:
            logger.debug(
                "Could not inspect napari's progress registry",
                exc_info=True,
            )

        for progress_bar in bars:
            with contextlib.suppress(Exception):
                progress_bar.close()
        self._napari_progress_bars = set()
        self._napari_progress = None
        self._progress_phase = "idle"

    def _browse_omezarr_dir(self) -> None:
        """Open a directory picker for the OME-ZARR output folder."""
        directory = QFileDialog.getExistingDirectory(
            self.dropdown_widget,
            "Select OME-ZARR Output Directory",
            self.omezarr_dir_edit.text(),
        )
        if directory:
            self.omezarr_dir_edit.setText(directory)

    def _create_plugin_omezarr_config(
        self,
        experiment_name: str,
    ) -> Any:
        """Build one OME-ZARR run configuration without an INI file."""
        from napari_zen_streaming.ZEN_omezarr import (
            create_plugin_experiment_config,
        )

        raw_compression = self.compression_combo.currentText()
        compression = raw_compression if raw_compression != "none" else None
        return create_plugin_experiment_config(
            experiment_name=experiment_name,
            output_dir=self.omezarr_dir_edit.text().strip(),
            dtype=self.config.pixel_dtype.name,
            compression=compression,
            zenapi_config=self.config.config_file,
            start_from_script=self.chk_auto_trigger.isChecked(),
            use_hcs_layout=(self.chk_hcs_layout.isVisible() and self.chk_hcs_layout.isChecked()),
            keep_source_tiles=self.chk_keep_source_tiles.isChecked(),
            pyramid_levels=(self.spin_pyramid_levels.value() if self.chk_create_pyramid.isChecked() else 0),
        )

    async def _load_selected_experiment_metadata(self, experiment_name: str) -> Any:
        """Load acquisition dimensions from the selected ZEN experiment."""
        if self.connection is None:
            raise RuntimeError("ZEN connection is not available.")
        from napari_zen_streaming.ZEN_omezarr import load_experiment_acquisition

        return await load_experiment_acquisition(
            self.connection.experiment_service,
            experiment_name,
        )

    def _on_experiment_changed(self, experiment_name: str) -> None:
        """Reload XML-derived acquisition metadata after selection changes."""
        if not experiment_name or self.connection is None:
            return

        self._metadata_request_id += 1
        request_id = self._metadata_request_id

        async def load_metadata() -> None:
            try:
                metadata = await self._load_selected_experiment_metadata(experiment_name)
                if request_id != self._metadata_request_id:
                    return
                self.signals.experiment_metadata_loaded.emit(metadata)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Could not load metadata for experiment %s: %s",
                    experiment_name,
                    exc,
                    exc_info=True,
                )

        self.safe_execution(load_metadata)

    def refresh_selected_experiment_metadata(self) -> None:
        """Parse XML metadata for the current experiment selection."""
        self._on_experiment_changed(self.dropdown.currentText())

    def _apply_experiment_metadata(self, metadata: Any) -> None:
        """Display and cache dimensions returned by the ZEN XML parser."""
        dimensions = metadata
        if dimensions.experiment_name != self.dropdown.currentText():
            return
        self._selected_experiment_metadata = dimensions
        self.spin_t.setValue(dimensions.time_points)
        self.spin_c.setValue(dimensions.channels)
        self.spin_z.setValue(dimensions.z_planes)
        self.spin_z_spacing.setValue(dimensions.z_spacing)
        self._z_spacing_um = dimensions.z_spacing
        self.label_tiles.setValue(dimensions.tiles)
        self.label_scenes.setValue(dimensions.scenes)
        self._on_dim_changed()
        self.dim_panel.setVisible(self.omezarr_enabled)

        self.chk_hcs_layout.setChecked(True)
        self._update_hcs_layout_option()

        logger.info(
            "Selected experiment metadata: "
            f"T={dimensions.time_points} C={dimensions.channels} "
            f"Z={dimensions.z_planes} M={dimensions.tiles} "
            f"S={dimensions.scenes}"
        )

    def safe_execution(self, fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """
        Helper function to safely schedule async function in appropriate event loop.

        Args:
            fn: Async function to execute (no arguments)
        Note:
            - If in embedded mode (plugin), uses worker thread loop
            - If in standalone mode, uses main thread loop
            - Handles absence of running loop gracefully
        """

        worker_loop = getattr(self.app, "worker_loop", None)

        if worker_loop is not None and not worker_loop.is_closed():
            # Embedded mode (plugin) - schedule in worker thread loop
            asyncio.run_coroutine_threadsafe(fn(), worker_loop)
        else:
            # Standalone mode - schedule in main loop
            try:
                loop = asyncio.get_running_loop()
                logger.debug("Scheduling experiment start in main loop (standalone mode)")
                loop.create_task(fn())
            except RuntimeError as e:
                # No running loop - this shouldn't happen
                logger.error(f"No running event loop available: {e}")

    def on_start_clicked(self):
        """
        Handle start button click - schedules async work in worker loop.

        This method runs in the Qt main thread. It captures the selected experiment
        name immediately (since Qt widgets can only be accessed from main thread),
        then schedules the actual experiment start in the appropriate event loop.

        Thread Safety:
        - Runs in main Qt thread
        - Captures Qt widget values before scheduling async work
        - Uses asyncio.run_coroutine_threadsafe for embedded mode
        - Uses asyncio.create_task for standalone mode
        """
        # Get experiment name NOW while we're in main Qt thread
        exp_name = self.dropdown.currentText()
        if not exp_name:
            logger.warning("No experiment selected")
            return

        if self.omezarr_enabled:
            self._zarr_ecfg = self._create_plugin_omezarr_config(exp_name)

        if self.app is None:
            logger.error("App reference not available")
            return

        if self.display_enabled:
            self._freeze_live_preview_option()

        # Clear existing layers immediately when user clicks start
        # This ensures clean slate for new experiment
        logger.debug("Clearing viewer for new experiment start")
        self._clear_layers()
        self._close_progress_bar()

        # Reset frame data structures
        self.frame_data.clear()
        self.frame_metadata_by_key.clear()
        self.frame_keys.clear()
        self.unique_channels.clear()
        self.is_streaming = True

        # Reset on-the-fly zarr write state; apply UI widget overrides
        # to the cached ExperimentConfig.
        self._reset_zarr_state()

        # In OME-ZARR only mode the standalone writer handles
        # everything.  Set the guard flag EARLY (before any async
        # scheduling) so the pipeline's add_frame path discards
        # frames even if the pipeline delivers frames between now
        # and when the standalone coroutine starts.  Also clear
        # _zarr_write_mode so the pipeline processor does not
        # attempt to open its own zarr stream.
        if not self.display_enabled and self.omezarr_enabled:
            self._standalone_zarr_active = True
            self._zarr_write_mode = False
            # Cancel any restructure/finalization task that a
            # straggler frame may have started before the flag
            # was set.
            if self.restructure_task and not self.restructure_task.done():
                self.restructure_task.cancel()
        else:
            self._zarr_write_mode = self.omezarr_enabled

        # Disable button immediately (we're in main thread, can access Qt widgets)
        self.signals.show_start_button.emit(False)

        logger.debug(f"User selected experiment: {exp_name}")

        # ---- OME-ZARR only mode: bypass the pipeline entirely ----
        # Use the same standalone function as the CLI script.
        # It creates its own gRPC channel, reads frames in a tight
        # loop and writes to zarr on-the-fly.  No queue, no
        # pipeline, no restructure task — just like running
        # ``python ZEN_stream2omezarr.py --experiment-config …``.
        if not self.display_enabled and self.omezarr_enabled:
            if self._zarr_ecfg is None:
                logger.error("Could not create the OME-ZARR run configuration.")
                self.signals.show_start_button.emit(True)
                return

            ecfg = self._zarr_ecfg
            base_ecfg = ecfg

            async def run_omezarr_standalone():
                """Run the standalone OME-ZARR writer.

                Uses the same ``stream_to_omezarr_with_config``
                function as the CLI, so behaviour is identical.

                The pipeline reader is suspended first so that only
                one ``monitor_all_experiments`` gRPC consumer exists
                while the zarr writer is active.  ZEN distributes
                frames across all active consumers, so a second
                consumer (the pipeline) causes frame loss in both.
                """
                pipeline = getattr(self.app, "pipeline", None)
                try:
                    from napari_zen_streaming.ZEN_stream2omezarr import (
                        stream_to_omezarr_with_config,
                    )

                    metadata = self._selected_experiment_metadata
                    if metadata is None or metadata.experiment_name != exp_name:
                        metadata = await self._load_selected_experiment_metadata(exp_name)
                    ecfg = replace(
                        base_ecfg,
                        time_points=metadata.time_points,
                        channels=metadata.channels,
                        z_planes=metadata.z_planes,
                        z_spacing=metadata.z_spacing,
                        tiles=metadata.tiles,
                        scenes=metadata.scenes,
                        positions=list(metadata.positions),
                    )
                    self.signals.experiment_metadata_loaded.emit(metadata)

                    # Cancel the pipeline's gRPC reader BEFORE opening
                    # the standalone writer's channel so ZEN has only
                    # one consumer and delivers all frames to it.
                    # suspend_reader() explicitly aclose()s the gRPC
                    # async iterator to send RST_STREAM.  A short sleep
                    # after that gives ZEN time to process the RST_STREAM
                    # and de-register the consumer before the standalone
                    # writer's new channel is registered.
                    if pipeline is not None:
                        await pipeline.suspend_reader()
                    await asyncio.sleep(0.5)

                    # Pass a progress callback so the napari activity
                    # dock shows a live frame counter.  The callback
                    # emits update_progress on the worker thread; the
                    # slot (_update_progress_slot) creates and updates
                    # the napari progress bar on the main thread.
                    progress_emitter = _ThrottledProgressEmitter(self.signals.update_progress.emit)

                    self.signals.show_stop_button.emit(True)
                    zarr_path = await stream_to_omezarr_with_config(
                        ecfg,
                        progress_callback=progress_emitter,
                    )
                    logger.info(f"OME-ZARR standalone complete: " f"{zarr_path}")
                    self.signals.open_omezarr.emit(str(zarr_path))
                except Exception as exc:
                    logger.error(
                        "OME-ZARR standalone failed: " f"{exc}",
                        exc_info=True,
                    )
                finally:
                    self._standalone_zarr_active = False
                    # Restart the pipeline reader so Display mode
                    # works again for the next experiment.
                    if pipeline is not None:
                        await pipeline.resume_reader()
                    self.signals.show_stop_button.emit(False)
                    self.signals.show_start_button.emit(True)

            try:
                self.safe_execution(run_omezarr_standalone)
            except Exception as e:
                logger.error(
                    f"Failed to schedule OME-ZARR standalone: {e}",
                    exc_info=True,
                )
                self._standalone_zarr_active = False
                self.signals.show_start_button.emit(True)
            return

        # ---- Display mode: use pipeline + status monitor ----
        # Create coroutine with exp_name captured
        async def start_experiment_async():
            """
            Start experiment in appropriate event loop.

            This coroutine runs in an event loop (either worker loop or main loop),
            so it can safely make async ZEN API calls. It must NOT access Qt widgets directly.
            """
            try:
                logger.debug(f"Starting experiment from UI: {exp_name}")

                pipeline = getattr(self.app, "pipeline", None)
                assert self.connection is not None, "connection must be set before starting an experiment"
                if pipeline is None:
                    raise RuntimeError("Streaming pipeline is not available")

                # Keep the perpetual all-experiments transport open, arm its
                # client-side ID filter, and only then start acquisition.
                self.current_experiment_id = await pipeline.start_targeted_experiment(exp_name)
                logger.debug(f"Experiment started with ID: {self.current_experiment_id}")

                self.signals.show_stop_button.emit(True)
                # Start monitoring experiment status via ZEN API
                if self.status_monitor_task and not self.status_monitor_task.done():
                    self.status_monitor_task.cancel()
                self.status_monitor_task = asyncio.create_task(self._monitor_experiment_status())

            except Exception as e:
                pipeline = getattr(self.app, "pipeline", None)
                if pipeline is not None:
                    pipeline.set_target_experiment(None)
                self.current_experiment_id = None
                logger.error(f"Failed to start experiment: {e}", exc_info=True)
                # Reset button on error (use QTimer for thread safety)
                self.signals.show_start_button.emit(True)
                QTimer.singleShot(0, lambda: self.start_button.setText("Start Selected Experiment (Error)"))
                raise

        try:
            self.safe_execution(start_experiment_async)
        except Exception as e:
            logger.error(f"Failed to schedule experiment start: {e}", exc_info=True)
            QTimer.singleShot(0, lambda: self.start_button.setVisible(True))
            self.start_button.setText("Start Selected Experiment (No Loop)")

    def on_stop_clicked(self):
        """
        Handle stop button click - stops the running experiment.

        This method runs in the Qt main thread. It disables the stop button
        and re-enables the start button. It also cancels any ongoing status
        monitoring task.

        Thread Safety:
        - Runs in main Qt thread
        - Safe to access and modify Qt widgets
        """
        logger.debug("Stop button clicked - stopping experiment")

        # Disable stop button immediately
        self.signals.show_stop_button.emit(False)
        self._close_progress_bar()

        # Cancel status monitoring if running
        if self.status_monitor_task and not self.status_monitor_task.done():
            self.status_monitor_task.cancel()
            logger.debug("Cancelled status monitoring task")

        async def stop_experiment_async():
            """ """
            try:
                if self.current_experiment_id is None:
                    logger.warning("Stop requested but no active experiment ID — nothing to stop.")
                    self.signals.show_start_button.emit(True)
                    return
                assert self.connection is not None, "connection must be set before stopping an experiment"
                await self.connection.stop_experiment(self.current_experiment_id)
                logger.debug(f"Experiment stopped with ID: {self.current_experiment_id}")

                # Re-enable start button
                self.signals.show_start_button.emit(True)

            except Exception as e:
                logger.error(f"Failed to stop experiment: {e}", exc_info=True)
                # Reset button on error (use QTimer for thread safety)
                self.signals.show_stop_button.emit(True)
                raise

        try:
            self.safe_execution(stop_experiment_async)
        except Exception as e:
            logger.error(f"Failed to schedule experiment stop: {e}", exc_info=True)
            self.signals.show_stop_button.emit(True)

        # Clear current experiment ID
        self.current_experiment_id = None

    def populate_experiments(self, experiments: list[str]) -> None:
        """Populate experiments and load the initial selection metadata.

        Must be called from Qt main thread.

        Args:
            experiments: List of experiment names from ZEN API

        Note:
            This is not async - it directly updates Qt widgets,
            so it must be called from the main thread.
        """
        previous_state = self.dropdown.blockSignals(True)
        try:
            self.dropdown.clear()
            self.dropdown.addItems(sorted(experiments, key=str.casefold))

            # Pre-select the configured experiment when it is available.
            if self.config.exp_name:
                idx = self.dropdown.findText(self.config.exp_name)
                if idx >= 0:
                    self.dropdown.setCurrentIndex(idx)
        finally:
            self.dropdown.blockSignals(previous_state)

        # Bulk population suppresses transient selection signals. Trigger one
        # request for the final selection so T/C/Z/M/S and total frames are
        # correct as soon as the plugin opens.
        self.refresh_selected_experiment_metadata()

    async def _monitor_experiment_status(self):
        """
        Monitor experiment status via ZEN API (napari-started mode only).

        Uses the ZEN API's register_on_status_changed to receive status updates.
        When the experiment finishes, triggers restructure and UI reset.
        This runs in the worker thread event loop.
        Note:
            Only used for napari-started experiments where we have an experiment_id.
            ZEN-started experiments use timeout-based detection instead.
        """
        if not self.current_experiment_id or not self.connection:
            logger.warning("Cannot monitor experiment status: missing experiment ID or connection")
            return

        try:
            from zen_api.acquisition.v1beta import ExperimentServiceRegisterOnStatusChangedRequest

            logger.debug(f"Starting status monitoring for experiment: {self.current_experiment_id}")

            # Register for status updates from ZEN API
            api_method = self.connection.experiment_service.register_on_status_changed(
                ExperimentServiceRegisterOnStatusChangedRequest(self.current_experiment_id)
            )

            # Stream status updates until experiment completes
            while True:
                # Stream is closed after 60 seconds of inactivity
                response = await asyncio.wait_for(api_method.__anext__(), timeout=60)

                # Check if experiment has stopped running
                if not response.status.is_experiment_running:
                    logger.warning("Experiment status changed: experiment no longer running")

                    self.signals.show_stop_button.emit(False)
                    # In OME-ZARR only mode do NOT trigger
                    # finalization from the status monitor.
                    # The experiment-finished signal from ZEN
                    # arrives before all pixel data has been
                    # delivered by the gRPC stream.  Finalization
                    # is handled by the frame-count trigger in
                    # _process_buffer or the inactivity timeout
                    # in _check_for_restructure – matching the
                    # behaviour of the standalone CLI script.
                    if not self.display_enabled and self.omezarr_enabled:
                        logger.info("OME-ZARR only mode: deferring finalization to frame-count / timeout triggers.")
                    else:
                        received = await self._wait_for_display_stream_completion()
                        logger.info(
                            "Finalizing Display mode with %d unique frames",
                            received,
                        )
                        await self.perform_restructure()
                        self.signals.experiment_finished.emit()
                    break

                logger.debug(
                    f"Experiment running: {response.status.is_experiment_running}, "
                    f"Acquisition running: {response.status.is_acquisition_running}"
                )

        except asyncio.TimeoutError:
            logger.warning("Experiment status monitoring timed out")
            self.signals.experiment_finished.emit()
        except asyncio.CancelledError:
            logger.debug("Experiment status monitoring cancelled")
        except Exception as e:
            logger.error(f"Error monitoring experiment status: {e}", exc_info=True)
            self.signals.experiment_finished.emit()

    def _set_start_button_enabled(self, enabled: bool) -> None:
        """Enable or disable the start button with matching text.

        When disabling the button the label reflects whether the plugin
        has already triggered the experiment (auto-trigger on) or is
        waiting for the user to start it manually in ZEN Blue
        (auto-trigger off).  Once the first pixel data arrives the
        label is updated to "Experiment & Stream Running" via the
        progress-update slot.

        Args:
            enabled: True to enable (idle state), False to disable
                (active / armed state).
        """
        self.start_button.setEnabled(enabled)
        if enabled:
            # Restore the correct idle label for the current mode.
            self._sync_start_button_label()
        else:
            # OME-ZARR mode without auto-trigger: writer is armed but
            # no experiment is running yet in ZEN Blue.
            if self.omezarr_enabled and not self.chk_auto_trigger.isChecked():
                self.start_button.setText("Writer Armed — start ZEN Experiment")
            else:
                self.start_button.setText("Experiment Running...")

    def _experiment_finished_slot(self):
        """
        Qt slot called when experiment finishes (runs in main thread).

        Resets the UI button to allow starting a new experiment.

        Thread Safety:
        - This is a Qt slot, guaranteed to run in main thread
        - Safe to access and modify Qt widgets
        """
        logger.debug("Experiment finished - resetting UI button")
        self.signals.show_start_button.emit(True)
        self._close_progress_bar()

        # Cancel status monitoring if still running
        if self.status_monitor_task and not self.status_monitor_task.done():
            self.status_monitor_task.cancel()

        self.current_experiment_id = None
        pipeline = getattr(self.app, "pipeline", None)
        if pipeline is not None:
            pipeline.set_target_experiment(None)
        if not self.is_streaming:
            self._release_live_preview_option()

    async def add_frame(self, image: np.ndarray, metadata: ImageMetadata) -> None:
        """
        Add a frame to the buffer for display (called from worker thread).

        This method is called by the pipeline's processor task for each frame
        received from ZEN. It buffers frames and schedules periodic updates
        to the napari viewer.

        Args:
            image: Image data to display (2D numpy array)
            metadata: Associated metadata (position, scaling, etc.)

        Thread Safety:
        - Called from worker thread
        - Uses Qt signals to communicate with main thread
        - Uses QTimer.singleShot for thread-safe Qt operations

        Note:
            Frames are buffered to reduce update frequency and improve performance.
            The actual viewer update happens asynchronously in batches.
        """
        self.image_buffer.append(image)
        self.metadata_buffer.append(metadata)

        # Update last frame time for timeout detection
        self.last_frame_time = time.time()

        # When the standalone OME-ZARR writer is active it has its
        # own gRPC channel and handles all frame processing.  The
        # pipeline still delivers frames here (it cannot be paused),
        # but we discard them to avoid a second zarr writer running
        # in parallel.
        if self._standalone_zarr_active:
            self.image_buffer.clear()
            self.metadata_buffer.clear()
            return

        # In OME-ZARR only mode, frames must be processed
        # exclusively by the standalone writer launched via the
        # Start button.  If the user starts an experiment directly
        # in ZEN without clicking Start, the pipeline reader picks
        # up the frames but the standalone writer was never armed.
        # Silently discard these frames — the user needs to click
        # Start first so the dedicated writer channel is set up.
        if not self.display_enabled and self.omezarr_enabled:
            self.image_buffer.clear()
            self.metadata_buffer.clear()
            if not hasattr(self, "_omezarr_discard_warned"):
                logger.warning(
                    "OME-ZARR only mode: discarding frames "
                    "from pipeline.  Use the Start button to "
                    "arm the standalone writer first."
                )
                self._omezarr_discard_warned = True
            return

        # ------- Post-restructure frame handling -------
        # After restructure sets is_streaming=False, incoming frames
        # are either (a) stragglers from the finished experiment, or
        # (b) the start of a genuinely new ZEN-started acquisition.
        # A short grace period distinguishes the two cases.
        if not self.is_streaming:
            elapsed = time.time() - self._restructure_completed_at
            if elapsed < _STRAGGLER_GRACE_SECONDS:
                logger.debug(
                    "Dropping straggler frame (%.1fs after restructure)",
                    elapsed,
                )
                self.image_buffer.pop()
                self.metadata_buffer.pop()
                return

            # Past the grace period → genuine new acquisition
            logger.info(
                "New ZEN-started acquisition detected " "(%.1fs after last restructure)",
                elapsed,
            )
            if self.image_layers or self.image_layer or self.frame_data or self._zarr_stream is not None:
                self.signals.clear_all_layers.emit()
                self.image_layers.clear()
                self.frame_data.clear()
                self.frame_metadata_by_key.clear()
                self.frame_keys.clear()
                self.unique_channels.clear()
                self.image_layer = None
                self._reset_zarr_state()
                self._zarr_write_mode = self.omezarr_enabled
            self.is_streaming = True

        if self.display_enabled:
            self._freeze_live_preview_option()

        # Disable button when first frame arrives
        if hasattr(self, "start_button"):
            self.signals.show_start_button.emit(False)

        # Cancel any existing restructure task and start a new one.
        # This ensures the timeout always counts from the LAST received
        # frame.  In OME-ZARR only mode finalization is driven by the
        # frame-count trigger in _process_buffer, so the restructure
        # task acts purely as an inactivity safety-net — we only need
        # to restart it when no task is running yet.
        omezarr_only = not self.display_enabled and self.omezarr_enabled
        if omezarr_only:
            if self.restructure_task is None or self.restructure_task.done():
                self.restructure_task = asyncio.create_task(self._check_for_restructure())
        else:
            if self.restructure_task and not self.restructure_task.done():
                self.restructure_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.restructure_task
            self.restructure_task = asyncio.create_task(self._check_for_restructure())

        # Process buffer with lock protection
        if not self.is_processing:
            self.is_processing = True
            asyncio.create_task(self._process_buffer_with_lock())

    def _clear_layers(self):
        """
        Clear all layers from napari viewer.

        Must be called from Qt main thread.

        Note:
            This is typically called when starting a new acquisition
            to remove old data from the viewer.
        """
        num_layers = len(self.viewer.layers)
        logger.info(f"Clearing {num_layers} layers from viewer")
        if num_layers > 0:
            for layer in list(self.viewer.layers):
                self.viewer.layers.remove(layer)
            logger.debug("All layers cleared from viewer")

        # Reset layer references so _create_layer_slot creates a fresh layer
        # and _restructure_complete_slot does not try to remove a stale one.
        self.image_layer = None
        self.image_layers.clear()

        # Close any open napari progress bar from the previous acquisition
        if self._napari_progress is not None:
            with contextlib.suppress(Exception):
                self._napari_progress.close()
            self._napari_progress = None

    async def _process_buffer_with_lock(self) -> None:
        """Wrapper to ensure only one buffer processing happens at a time."""
        async with self.processing_lock:
            await self._process_buffer()

    async def _process_buffer(self) -> None:
        """Store buffered frames and update the live preview.

        Every frame is retained in the sparse ``frame_data`` mapping for final
        scene assembly. When latest-frame preview is enabled, only the newest
        2D image in each batch crosses the Qt boundary. The legacy mode instead
        rebuilds and emits the complete 7D acquisition history.

        Thread Safety:
        - Runs in worker thread
        - Emits signals for main thread to handle viewer updates

        Note:
            Errors in individual batch processing are logged but don't stop the pipeline.
        """
        try:
            # Give other tasks a chance to run
            await asyncio.sleep(0.001)

            if not self.image_buffer:
                return

            # Get all buffered images
            images = list(self.image_buffer)
            logger.debug(f"Processing {len(images)} buffered frames")
            metadata_list = list(self.metadata_buffer)
            self.image_buffer.clear()
            self.metadata_buffer.clear()

            # In OME-ZARR mode, write frames directly to disk;
            # skip frame_data buffering and layer updates.
            if self.omezarr_enabled:
                all_received = self._process_zarr_frames(images, metadata_list)
                if all_received and not self.display_enabled and not self._zarr_finalizing:
                    logger.info("All expected frames received - scheduling zarr finalization.")
                    asyncio.create_task(self.finalize_zarr_acquisition())
                return

            if not self.frame_data:
                # First frame - create layer
                first_image = images[0]
                first_metadata = metadata_list[0]

                # Store frame data
                key = (
                    first_metadata.frame_s,
                    first_metadata.frame_t,
                    first_metadata.frame_m,
                    first_metadata.frame_z,
                    first_metadata.frame_c,
                )
                self.frame_data[key] = first_image.copy()
                self.frame_metadata_by_key[key] = first_metadata
                self.image_shape = first_image.shape
                self.unique_channels.add(first_metadata.frame_c)
                self.scaling_y_um = first_metadata.scaling_y_um
                self.scaling_x_um = first_metadata.scaling_x_um
                self._update_frame_keys()

                # Emit streaming progress in OME-ZARR mode
                if self.omezarr_enabled:
                    total = self._expected_total_frames or 0
                    self.signals.update_progress.emit(1, total)

                # The Qt slot chooses either a 2D latest-frame layer or the
                # legacy 7D history layer from the frozen run option.
                if self.display_enabled:
                    self.signals.create_layer.emit(first_image, first_metadata)

                # Remove first frame from batch
                images = images[1:]
                metadata_list = metadata_list[1:]

            if images:
                # Store remaining frames
                for image, metadata in zip(images, metadata_list, strict=False):
                    key = (metadata.frame_s, metadata.frame_t, metadata.frame_m, metadata.frame_z, metadata.frame_c)
                    self.frame_data[key] = image.copy()
                    self.frame_metadata_by_key[key] = metadata
                    self.unique_channels.add(metadata.frame_c)

                self._update_frame_keys()

                # Emit streaming progress in OME-ZARR mode
                if self.omezarr_enabled:
                    total = self._expected_total_frames or 0
                    self.signals.update_progress.emit(
                        len(self.frame_data),
                        total,
                    )

                if self.display_enabled:
                    if self._run_live_latest:
                        preview_data = images[-1]
                    else:
                        preview_data = self._build_dense_array()
                    self.signals.update_layer.emit(
                        preview_data,
                        metadata_list,
                    )

        except Exception as e:
            logger.error(f"Error processing image buffer: {e}", exc_info=True)
        finally:
            self.is_processing = False

    def _create_layer_slot(self, image: np.ndarray, metadata: ImageMetadata):
        """Create the temporary streaming layer on the Qt thread.

        Latest-frame mode creates a 2D YX layer. Legacy mode creates the
        complete 7D linear history layer. Both modes use the sparse frame
        store as the source for final STZYX scene assembly.

        Args:
            image: First image (used for contrast limits)
            metadata: First image metadata

        Thread Safety:
        - This is a Qt slot, guaranteed to run in main thread
        - Safe to create napari layers and modify viewer
        """
        # Safety check: if layer already exists, don't create another
        if self.image_layer is not None:
            logger.warning("Layer already exists, skipping duplicate creation")
            return

        latest_only = bool(self._run_live_latest)
        data_array = image if latest_only else self._build_dense_array()

        logger.debug(
            "Created %s live layer with shape %s",
            "latest-frame" if latest_only else "full-history",
            data_array.shape,
        )

        # Create single streaming layer
        self.image_layer = cast(
            Image,
            self.viewer.add_image(
                data_array,
                name="Streaming",
                contrast_limits=(image.min(), image.max()),
                colormap="gray",
            ),
        )

        if latest_only:
            self.viewer.dims.axis_labels = ("Y", "X")
        else:
            self.viewer.dims.axis_labels = (
                "Frames",
                "T",
                "M",
                "Z",
                "C",
                "Y",
                "X",
            )
            self.viewer.dims.set_current_step(0, 0)

        logger.debug("Initialized viewer with streaming layer")

    def _update_layer_slot(self, data_array: np.ndarray, metadata_list: list):
        """Update the temporary streaming layer on the Qt thread.

        ``data_array`` is either the newest 2D image or the complete 7D frame
        history, according to the option frozen at acquisition start.

        Args:
            data_array: Complete 7D array with all frames acquired so far
            metadata_list: List of metadata for the newly added frames

        Thread Safety:
        - This is a Qt slot, guaranteed to run in main thread
        - Safe to update napari layers and viewer state
        """
        if not self.image_layer:
            logger.warning("Attempted to update layer but layer doesn't exist")
            return

        last_metadata = metadata_list[-1]
        logger.debug(
            f"Appended {len(metadata_list)} frames. Last frame - "
            f"S:{last_metadata.frame_s} T:{last_metadata.frame_t} "
            f"M:{last_metadata.frame_m} Z:{last_metadata.frame_z} C:{last_metadata.frame_c}"
        )

        self.image_layer.data = data_array  # type: ignore[assignment]

        if self._run_live_latest:
            self.viewer.dims.axis_labels = ("Y", "X")
            return

        # Update axis labels (streaming mode)
        self.viewer.dims.axis_labels = ("Frames", "T", "M", "Z", "C", "Y", "X")
        # Move to last frame (most recent data)
        self.viewer.dims.set_current_step(0, data_array.shape[0] - 1)

        # Update contrast limits based on latest image
        latest = metadata_list[-1]
        latest_image = self.frame_data[(latest.frame_s, latest.frame_t, latest.frame_m, latest.frame_z, latest.frame_c)]
        self.image_layer.contrast_limits = (latest_image.min(), latest_image.max())

    def _update_frame_keys(self) -> None:
        """
        Update sorted list of frame keys (including all 5 dimensions: S, T, M, Z, C).

        Maintains frame keys in sorted order for predictable array construction.
        Called whenever new frames are added to frame_data.
        """
        # Sort all keys in STMZC order
        self.frame_keys = sorted(self.frame_data.keys())

    def _build_dense_array(self) -> np.ndarray:
        """
        Build a 7D array where total_frames = size_s * size_t * size_m * size_z * size_c.
        Every position contains actual acquired data - NO ZEROS!

        During streaming: Uses linear layout (all frames in first dimension)

        Returns:
            Dense 7D array (STMZCYX) with shape (N, 1, 1, 1, 1, Y, X) during streaming

        Note:
            This should only be called during streaming. After restructure,
            data is organized into separate STZYX scene mosaics per channel.

        Raises:
            RuntimeError: If called when not in streaming mode
        """
        logger.debug("Building dense 7D array from sparse frame data...")
        total_frames = len(self.frame_keys)

        # Get dtype from first frame
        first_frame = self.frame_data[self.frame_keys[0]]

        if self.is_streaming:
            # During streaming: use simple linear layout (N, 1, 1, 1, 1, Y, X)
            assert self.image_shape is not None, "image_shape must be set before building array"
            shape = (total_frames, 1, 1, 1, 1, self.image_shape[0], self.image_shape[1])
            data_array = np.zeros(shape, dtype=first_frame.dtype)

            # Fill frames linearly
            for linear_idx, key in enumerate(self.frame_keys):
                data_array[linear_idx, 0, 0, 0, 0, :, :] = self.frame_data[key]

            logger.debug(f"Built linear 7D array: {shape}")
        else:
            # This should never happen in streaming-only mode
            # Restructure is handled by _check_for_restructure which creates separate layers
            raise RuntimeError("_build_dense_array should only be called during streaming")

        return data_array

    async def _check_for_restructure(self) -> None:
        """
        Check if streaming has stopped and trigger finalization.

        Three modes of operation:

        1. **Napari-started, display mode**: Waits indefinitely.
           The status monitor handles restructure; this task exists
           only to be cancelled/restarted on each new frame.

        2. **Napari-started, OME-ZARR only mode**: Uses an
           inactivity timeout (polls ``last_frame_time``).  The
           status monitor intentionally does NOT trigger
           finalization because ZEN signals experiment-finished
           before all pixel data has been delivered via gRPC.

        3. **ZEN-started (any mode)**: Same inactivity timeout.

        When streaming stops, triggers either
        ``finalize_zarr_acquisition`` (OME-ZARR only mode) or
        ``perform_restructure`` (display mode).

        Note:
            In display mode (case 1) this task is cancelled and
            restarted every time a new frame arrives.  In OME-ZARR
            only mode (case 2) the task runs for the entire
            acquisition and polls ``last_frame_time`` internally,
            avoiding per-frame cancel/recreate overhead.
        """
        try:
            if (
                not self.image_layer
                and not self.frame_data
                and self._zarr_stream is None
                and self._zarr_frame_count == 0
            ):
                logger.debug("No streaming layer, frame data or zarr - stream exists, skipping restructure check")
                return

            # For napari-started experiments in display mode,
            # status monitoring handles restructure so we wait
            # indefinitely (this task exists only to be cancelled
            # by new frames).  In OME-ZARR only mode we always
            # use a timeout — just like the standalone CLI script
            # — because the experiment-finished signal arrives
            # before all pixel data has been delivered.
            if self.current_experiment_id and not (not self.display_enabled and self.omezarr_enabled):
                logger.debug("Napari-started experiment: status monitoring will trigger restructure")
                await asyncio.Event().wait()  # Never completes, only cancelled
            else:
                # Timeout-based detection.  Used for ZEN-started
                # experiments (no experiment_id) and for
                # napari-started OME-ZARR only mode (status monitor
                # intentionally does not trigger finalization).
                # Loop until inactivity exceeds the threshold,
                # re-checking every second so the task doesn't
                # need to be cancelled/restarted on every frame.
                timeout = self.config.restructure_timeout
                logger.debug(f"Inactivity-timeout mode: waiting {timeout}s of silence ...")
                while True:
                    await asyncio.sleep(1.0)
                    # Bail out if the standalone OME-ZARR writer
                    # took over (it handles its own finalization).
                    if self._standalone_zarr_active:
                        logger.debug("Standalone zarr writer active - aborting pipeline restructure.")
                        return
                    idle = time.time() - self.last_frame_time
                    if idle >= timeout:
                        break

                if not self.display_enabled and self.omezarr_enabled:
                    logger.warning("Triggering zarr finalization - due to timeout")
                    await self.finalize_zarr_acquisition()
                else:
                    logger.warning("Triggering restructure - due to timeout")
                    await self.perform_restructure()
                    self.signals.experiment_finished.emit()

        except asyncio.CancelledError:
            # Task was cancelled because new frame arrived - this is normal
            logger.debug("Restructure check cancelled (new frame arrived)")
            raise  # Re-raise so task properly completes as cancelled
        except Exception as e:
            logger.error(f"Error during restructure check: {e}", exc_info=True)

    async def finalize_zarr_acquisition(self) -> None:
        """Finalize an OME-ZARR-only acquisition.

        Called when display is disabled and OME-ZARR writing is
        active.  Waits for remaining frames, drains the buffer,
        closes the zarr stream and opens the result in the viewer.

        This method is the OME-ZARR counterpart of
        ``perform_restructure`` (which handles display-mode
        7D-to-STZYX scene-mosaic layer conversion).

        Idempotent: subsequent calls after the first are no-ops.
        Normally triggered automatically when the frame count
        reaches ``_zarr_total_expected``.  The status monitor
        and timeout act as safety-net fallbacks.
        """
        # Idempotency guard – prevent double finalization.
        if self._zarr_finalizing:
            logger.debug("finalize_zarr_acquisition already " "running / completed – skipping.")
            return
        self._zarr_finalizing = True

        # Safety-net: if triggered by the status monitor before
        # all frames arrived, keep waiting as long as new frames
        # keep coming (inactivity-based, like the CLI script).
        # Each arriving frame resets the countdown via
        # ``last_frame_time``.
        if self._zarr_total_expected > 0 and self._zarr_frame_count < self._zarr_total_expected:
            inactivity_limit = 30.0  # seconds
            logger.info(
                f"Safety-net: waiting for remaining "
                f"frames ({self._zarr_frame_count}/"
                f"{self._zarr_total_expected}), "
                f"inactivity timeout={inactivity_limit}s"
            )
            while self._zarr_frame_count < self._zarr_total_expected:
                await asyncio.sleep(0.5)
                idle = time.time() - self.last_frame_time
                if idle >= inactivity_limit:
                    logger.warning(
                        f"No frames for {idle:.1f}s "
                        f"({self._zarr_frame_count}/"
                        f"{self._zarr_total_expected}) "
                        f"– giving up."
                    )
                    break
            else:
                logger.info("All expected frames received.")

        logger.debug("Finalizing OME-ZARR acquisition...")
        self.is_streaming = False
        self._restructure_completed_at = time.time()

        # Drain any frames still in the buffer.
        if self.image_buffer:
            logger.debug(f"Flushing {len(self.image_buffer)} buffered " "frames before finalizing.")
            async with self.processing_lock:
                buf_images = list(self.image_buffer)
                buf_meta = list(self.metadata_buffer)
                self.image_buffer.clear()
                self.metadata_buffer.clear()
                self._process_zarr_frames(buf_images, buf_meta)

        if self._zarr_stream is None and self._zarr_frame_count == 0:
            logger.warning("finalize_zarr_acquisition called but no " "zarr data exists, skipping...")
            return

        # Close the on-the-fly OME-ZARR stream.
        try:
            self._finalize_zarr_stream()
        except Exception as e:
            logger.error(
                f"Failed to finalise OME-ZARR: {e}",
                exc_info=True,
            )

        # Open the finished OME-ZARR in the viewer.
        if self._last_zarr_path:
            self.signals.open_omezarr.emit(str(self._last_zarr_path))

        # Close the streaming progress bar.
        self.signals.update_progress.emit(-2, 0)
        self.signals.experiment_finished.emit()

    async def perform_restructure(self) -> None:
        """Convert the streaming layer into STZYX scene mosaics.

        Called at the end of a display-mode acquisition to
        restructure the single streaming array into separate 5D layers
        (one per channel), merging each scene's M tiles spatially.

        This can be triggered by:
        - Status monitoring (napari-started experiments)
        - Timeout (ZEN-started experiments)

        After restructure, data is organized as:
        - One 5D layer per channel
        - Each layer has shape (S, T, Z, Y, X)
        - Higher M indices overwrite lower M indices in overlaps
        - Proper dimension labels for navigation
        """
        logger.debug("Starting restructure process...")
        self.is_streaming = False
        self._restructure_completed_at = time.time()

        # Drain any frames still buffered but not yet in
        # frame_data.
        if self.image_buffer:
            logger.debug(f"Flushing {len(self.image_buffer)} buffered " "frames before restructure.")
            async with self.processing_lock:
                buf_images = list(self.image_buffer)
                buf_meta = list(self.metadata_buffer)
                self.image_buffer.clear()
                self.metadata_buffer.clear()
                for image, metadata in zip(buf_images, buf_meta, strict=False):
                    key = (
                        metadata.frame_s,
                        metadata.frame_t,
                        metadata.frame_m,
                        metadata.frame_z,
                        metadata.frame_c,
                    )
                    self.frame_data[key] = image.copy()
                    self.frame_metadata_by_key[key] = metadata
                    self.unique_channels.add(metadata.frame_c)
                self._update_frame_keys()

        if not self.frame_data:
            logger.warning("Restructure called but no frame data " "exists, skipping...")
            return

        # Check if channel layers already exist
        if self.image_layers:
            logger.warning("Restructure called but channel layers " "already exist, skipping...")
            return

        # Finalise the on-the-fly OME-ZARR stream if enabled.
        if self.omezarr_enabled:
            try:
                self._finalize_zarr_stream()
            except Exception as e:
                logger.error(
                    f"Failed to finalise OME-ZARR: {e}",
                    exc_info=True,
                )

        if not self.image_layer:
            logger.warning("Restructure called but no streaming layer exists, skipping...")
            return

        tile_geometries: dict[tuple[int, int], TileGeometry] = {}
        for key in self.frame_keys:
            scene, _, tile, _, _ = key
            tile_key = (scene, tile)
            metadata = self.frame_metadata_by_key.get(key)
            if metadata is None or tile_key in tile_geometries:
                continue
            tile_geometries[tile_key] = TileGeometry(
                scene_index=scene,
                tile_index=tile,
                position_index=0,
                center_x=metadata.stage_x_um,
                center_y=metadata.stage_y_um,
                position_z=metadata.stage_z_um,
                scale_x=metadata.scaling_x_um,
                scale_y=metadata.scaling_y_um,
                height=self.frame_data[key].shape[0],
                width=self.frame_data[key].shape[1],
            )

        # Build channel data arrays
        channel_data = {}
        for channel_idx in sorted(self.unique_channels):
            # Get all frames for this channel
            channel_frames = [
                (s, t, m, z, self.frame_data[(s, t, m, z, channel_idx)])
                for (s, t, m, z, c) in self.frame_keys
                if c == channel_idx
            ]

            # Merge each scene's M tiles into one STZYX mosaic.
            data_array = assemble_channel_scene_mosaics(
                channel_frames,
                tile_geometries,
            )
            first_image = channel_frames[0][4]

            channel_data[channel_idx] = {
                "data": data_array,
                "first_image": first_image,
                "name": f"Channel {channel_idx}",
            }

        # Store channel data as instance variable for signal slot to access
        # This is a backup in case Qt signal transmission has issues with large numpy arrays
        self._pending_channel_data = channel_data

        # Emit signal to restructure in main thread
        self.signals.restructure_complete.emit(channel_data)

        # Close the streaming progress bar (if still open)
        self.signals.update_progress.emit(-2, 0)

    def _restructure_complete_slot(self, channel_data: dict):
        """
        Qt slot to complete restructure (runs in main thread).

        Removes the streaming layer and creates separate STZYX layers for each channel.
        Updates viewer dimensions and labels for proper multi-dimensional navigation.

        Args:
            channel_data: Dictionary mapping channel_idx to dict containing:
                         'data': 5D STZYX numpy array
                         'first_image': first frame for contrast limits
                         'name': layer name

        Thread Safety:
        - This is a Qt slot, guaranteed to run in main thread
        - Safe to modify napari viewer and layers
        """
        # Use instance variable if signal data is problematic
        if not channel_data and hasattr(self, "_pending_channel_data"):
            channel_data = self._pending_channel_data

        if not channel_data:
            logger.error("No channel data available for restructure")
            return

        # Guard: image_layers is populated here (main thread), so this check
        # is reliable even if perform_restructure emitted the signal twice
        # before the first slot invocation completed.
        if self.image_layers:
            logger.warning("Restructure slot called but channel layers already exist, ignoring duplicate signal.")
            return

        # Remove the streaming layer
        if self.image_layer and self.image_layer in self.viewer.layers:
            logger.debug(f"Removing streaming layer: {self.image_layer.name}")
            self.viewer.layers.remove(self.image_layer)
            self.image_layer = None

        logger.debug(
            f"Creating {len(self.unique_channels)} channel layers from channels: {sorted(self.unique_channels)}"
        )

        # Build physical scale for (S, T, Z, Y, X). S/T have no physical
        # step. Prefer the stage-Z positions carried by the active stream;
        # the selected experiment XML may be stale for ZEN-started runs.
        # XY pixel size is captured from the first streamed frame.
        z_spacing_um = _resolve_streamed_z_spacing_um(
            self.frame_metadata_by_key,
            fallback_um=self._z_spacing_um,
        )
        self._z_spacing_um = z_spacing_um
        logger.info("Final display Z spacing: %.6f µm", z_spacing_um)
        layer_scale = (
            1,
            1,
            z_spacing_um,
            self.scaling_y_um,
            self.scaling_x_um,
        )

        # Create separate 5D layers for each channel
        for channel_idx in sorted(channel_data.keys()):
            data = channel_data[channel_idx]

            # Create layer for this channel
            self.image_layers[channel_idx] = cast(
                Image,
                self.viewer.add_image(
                    data["data"],
                    name=data["name"],
                    contrast_limits=(data["first_image"].min(), data["first_image"].max()),
                    colormap="gray",
                    scale=layer_scale,
                ),
            )

            logger.debug(f"Created {data['name']} with shape (STZYX): " f"{data['data'].shape}, scale: {layer_scale}")

        # Update axis labels for 5D scene mosaics.
        self.viewer.dims.axis_labels = ("S", "T", "Z", "Y", "X")
        # Reset to first position in each dimension
        for axis in range(min(3, self.viewer.dims.ndim)):
            self.viewer.dims.set_current_step(axis, 0)

        logger.debug(f"Split into {len(self.unique_channels)} channel layers with STZYX scene mosaics")

        # Clean up
        if hasattr(self, "_pending_channel_data"):
            delattr(self, "_pending_channel_data")

        # Re-enable start button now that restructure is complete
        self.signals.experiment_finished.emit()

    def _reset_zarr_state(self) -> None:
        """Close any open zarr stream and clear on-the-fly write state.

        Safe to call from the worker thread or the main thread.  If the
        stream is open it is closed immediately (without writing pending
        frames), so callers should only invoke this when a clean abort
        is acceptable (e.g. when a new experiment replaces the current one).
        """
        if self._zarr_stream is not None:
            try:
                self._zarr_stream.__exit__(None, None, None)
            except Exception as exc:
                logger.warning(f"Error closing zarr stream during reset: {exc}")
            self._zarr_stream = None
        self._zarr_coord_to_linear = {}
        self._zarr_pending = {}
        self._zarr_next_write = 0
        self._zarr_total_expected = 0
        self._zarr_path = None
        self._zarr_frame_count = 0
        self._zarr_finalizing = False
        self._standalone_zarr_active = False
        # Allow the one-shot discard warning to fire again for the
        # next ZEN-started experiment in OME-ZARR only mode.
        if hasattr(self, "_omezarr_discard_warned"):
            del self._omezarr_discard_warned

    def _open_zarr_stream(
        self,
        frame_height: int,
        frame_width: int,
        scale_x_um: float,
        scale_y_um: float,
    ) -> bool:
        """Initialise the OME-ZARR write stream on the first received frame.

        Builds ``AcquisitionSettings`` from the cached ``_zarr_ecfg``,
        opens the ome-writers stream context manager, and populates
        ``_zarr_coord_to_linear`` and ``_zarr_total_expected``.
        Called lazily from ``_process_zarr_frames``.

        Args:
            frame_height: Height of each 2D frame in pixels.
            frame_width: Width of each 2D frame in pixels.
            scale_x_um: Pixel size in X (µm).
            scale_y_um: Pixel size in Y (µm).

        Returns:
            True if the stream was opened successfully, False otherwise.
        """
        from ome_writers import (
            AcquisitionSettings,
            Dimension,
            Position,
            create_stream,
        )

        ecfg = self._zarr_ecfg
        if ecfg is None:
            logger.error("OME-ZARR mode active but its run configuration " "was not created.")
            return False

        num_t = ecfg.time_points
        num_c = ecfg.channels
        num_z = ecfg.z_planes
        num_m = ecfg.tiles
        num_s = ecfg.scenes
        num_positions = num_s * num_m
        dtype = np.dtype(ecfg.dtype)
        output_dir = Path(ecfg.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._zarr_path = output_dir / f"{ecfg.experiment_name}_{timestamp}.ome.zarr"

        # Precompute (t, s, m, c, z) → linear_index.
        # Iteration order: T → P(S*M) → C → Z (matches ome-writers).
        self._zarr_coord_to_linear = {
            coord: idx
            for idx, coord in enumerate(
                itertools.product(
                    range(num_t),
                    range(num_s),
                    range(num_m),
                    range(num_c),
                    range(num_z),
                )
            )
        }

        dimensions: list[Dimension] = []
        if num_t > 1:
            dimensions.append(Dimension(name="t", count=num_t, chunk_size=1, type="time"))
        if num_positions > 1:
            pos_coords = [
                Position(
                    name=f"S{s_}_M{m_}",
                    grid_row=s_,
                    grid_column=m_,
                )
                for s_ in range(num_s)
                for m_ in range(num_m)
            ]
            dimensions.append(Dimension(name="p", type="position", coords=pos_coords))  # type: ignore[arg-type]
        if num_c > 1:
            dimensions.append(Dimension(name="c", count=num_c, chunk_size=1, type="channel"))
        if num_z > 1:
            dimensions.append(
                Dimension(
                    name="z",
                    count=num_z,
                    chunk_size=max(1, num_z),
                    type="space",
                    scale=ecfg.z_spacing,
                    unit="um",
                )
            )
        dimensions.append(
            Dimension(
                name="y",
                count=frame_height,
                chunk_size=min(512, frame_height),
                type="space",
                scale=scale_y_um,
                unit="um",
            )
        )
        dimensions.append(
            Dimension(
                name="x",
                count=frame_width,
                chunk_size=min(512, frame_width),
                type="space",
                scale=scale_x_um,
                unit="um",
            )
        )

        settings = AcquisitionSettings(
            root_path=str(self._zarr_path),
            dimensions=tuple(dimensions),
            dtype=str(dtype),
            format="ome-zarr",  # type: ignore[arg-type]
            compression=ecfg.compression,  # type: ignore[arg-type]
            overwrite=ecfg.overwrite_zarr,
        )

        self._zarr_total_expected = settings.num_frames
        self._expected_total_frames = settings.num_frames
        logger.info(
            f"Opening OME-ZARR stream: {self._zarr_path}  "
            f"shape={settings.shape}  "
            f"total_frames={self._zarr_total_expected}"
        )
        self._zarr_stream = create_stream(settings).__enter__()
        self._zarr_next_write = 0
        self._zarr_pending = {}
        self._zarr_frame_count = 0
        return True

    def _process_zarr_frames(
        self,
        images: list[np.ndarray],
        metadata_list: list[ImageMetadata],
    ) -> bool:
        """Write a batch of frames directly to the OME-ZARR stream.

        Opens the zarr stream lazily on the first frame using the
        cached ``_zarr_ecfg``.  Uses a small ``_zarr_pending``
        dict to handle out-of-order frame delivery and flushes
        frames consecutively in the ome-writers iteration order.
        Emits ``update_progress`` after each flush.

        Args:
            images: Batch of 2D frame arrays.
            metadata_list: Per-frame metadata (same length
                as images).

        Returns:
            ``True`` when all expected frames have been
            received, signalling that the caller should
            schedule ``finalize_zarr_acquisition``.
        """
        for image, metadata in zip(images, metadata_list, strict=False):
            # Lazily open the stream on the very first frame.
            if self._zarr_stream is None and not self._open_zarr_stream(
                image.shape[0],
                image.shape[1],
                metadata.scaling_x_um,
                metadata.scaling_y_um,
            ):
                # No config available – abort silently.
                return

            # Coordinate key matching coord_to_linear iteration order.
            key = (
                metadata.frame_t,
                metadata.frame_s,
                metadata.frame_m,
                metadata.frame_c,
                metadata.frame_z,
            )
            lin_idx = self._zarr_coord_to_linear.get(key)
            if lin_idx is None:
                logger.warning(f"Unexpected frame coordinate {key} – skipping.")
                continue

            self._zarr_frame_count += 1
            frame_meta = {
                "position_x": metadata.stage_x_um,
                "position_y": metadata.stage_y_um,
                "position_z": metadata.stage_z_um,
            }
            self._zarr_pending[lin_idx] = (image, frame_meta)

            # Flush all consecutively available frames in order.
            while self._zarr_next_write in self._zarr_pending:
                frm, fmeta = self._zarr_pending.pop(self._zarr_next_write)
                self._zarr_stream.append(frm, frame_metadata=fmeta)
                self._zarr_next_write += 1

            self.signals.update_progress.emit(
                self._zarr_next_write,
                self._zarr_total_expected,
            )

        # Signal the caller when all frames have been received.
        return self._zarr_total_expected > 0 and self._zarr_frame_count >= self._zarr_total_expected

    def _finalize_zarr_stream(self) -> None:
        """Flush all pending frames and close the OME-ZARR stream.

        Called from ``finalize_zarr_acquisition`` (OME-ZARR only
        mode) or ``perform_restructure`` (display + zarr mode)
        after the pixel stream ends.
        Fills any gaps in the frame buffer with ``skip`` calls for
        missing frames, then calls ``__exit__`` on the ome-writers
        context manager and stores the path in ``_last_zarr_path``.
        """
        if self._zarr_stream is None:
            return

        total = len(self._zarr_coord_to_linear)
        logger.info(
            f"Finalising OME-ZARR " f"({self._zarr_frame_count}/{self._zarr_total_expected} " "frames received)."
        )

        while self._zarr_next_write < total:
            if self._zarr_next_write in self._zarr_pending:
                frm, fmeta = self._zarr_pending.pop(self._zarr_next_write)
                self._zarr_stream.append(frm, frame_metadata=fmeta)
            else:
                self._zarr_stream.skip(frames=1)
                logger.warning("Missing frame at linear index " f"{self._zarr_next_write} – skipped.")
            self._zarr_next_write += 1
            self.signals.update_progress.emit(self._zarr_next_write, self._zarr_total_expected)

        self._zarr_stream.__exit__(None, None, None)
        self._zarr_stream = None
        self._last_zarr_path = self._zarr_path
        logger.info(f"OME-ZARR stream closed: {self._zarr_path}")

    def _write_omezarr(self) -> Path | None:
        """Write collected frame data to an OME-ZARR file.

        Uses the buffered ``self.frame_data`` dict that was populated
        during streaming.  Dimensions are discovered from the observed
        coordinate ranges (same approach as CLI-mode
        ``stream_to_omezarr``).

        The output path, compression and experiment name are taken from
        the current UI widget values.

        Returns:
            Path to the written OME-ZARR directory, or ``None`` when
            there is no frame data to write.

        Raises:
            ImportError: If ``ome_writers`` is not installed.
        """
        from ome_writers import (
            AcquisitionSettings,
            Dimension,
            Position,
            create_stream,
        )

        if not self.frame_data:
            logger.warning("No frame data to write.")
            return None

        # --- resolve output path ---
        output_dir = Path(self.omezarr_dir_edit.text())
        output_dir.mkdir(parents=True, exist_ok=True)
        exp_name = self.dropdown.currentText() or self.config.exp_name
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zarr_path = output_dir / f"{exp_name}_{timestamp}.ome.zarr"

        compression_raw = self.compression_combo.currentText()
        compression = compression_raw if compression_raw != "none" else None

        # --- discover dimension extents ---
        all_s = {k[0] for k in self.frame_keys}
        all_t = {k[1] for k in self.frame_keys}
        all_m = {k[2] for k in self.frame_keys}
        all_z = {k[3] for k in self.frame_keys}
        all_c = {k[4] for k in self.frame_keys}

        num_s = len(all_s)
        num_t = len(all_t)
        num_m = len(all_m)
        num_z = len(all_z)
        num_c = len(all_c)
        num_positions = num_s * num_m

        assert self.image_shape is not None
        frame_height, frame_width = self.image_shape
        first_frame = self.frame_data[self.frame_keys[0]]
        dtype = first_frame.dtype

        # --- build OME-ZARR dimensions ---
        dimensions: list[Dimension] = []

        if num_t > 1:
            dimensions.append(Dimension(name="t", count=num_t, chunk_size=1, type="time"))

        if num_positions > 1:
            sorted_s = sorted(all_s)
            sorted_m = sorted(all_m)
            pos_coords = [
                Position(
                    name=f"S{s_}_M{m_}",
                    grid_row=s_,
                    grid_column=m_,
                )
                for s_ in sorted_s
                for m_ in sorted_m
            ]
            dimensions.append(
                Dimension(
                    name="p",
                    type="position",
                    coords=pos_coords,  # type: ignore[arg-type]
                )
            )

        if num_c > 1:
            dimensions.append(Dimension(name="c", count=num_c, chunk_size=1, type="channel"))

        if num_z > 1:
            dimensions.append(
                Dimension(
                    name="z",
                    count=num_z,
                    chunk_size=max(1, num_z),
                    type="space",
                    scale=1.0,
                    unit="um",
                )
            )

        dimensions.append(
            Dimension(
                name="y",
                count=frame_height,
                chunk_size=min(512, frame_height),
                type="space",
                scale=self.scaling_y_um,
                unit="um",
            )
        )
        dimensions.append(
            Dimension(
                name="x",
                count=frame_width,
                chunk_size=min(512, frame_width),
                type="space",
                scale=self.scaling_x_um,
                unit="um",
            )
        )

        settings = AcquisitionSettings(
            root_path=str(zarr_path),
            dimensions=tuple(dimensions),
            dtype=str(dtype),
            format="ome-zarr",  # type: ignore[arg-type]
            compression=compression,  # type: ignore[arg-type]
            overwrite=True,
        )

        logger.info(f"Writing OME-ZARR: {zarr_path}  " f"shape={settings.shape}  frames={settings.num_frames}")

        # --- build coordinate-to-frame-index mapping ---
        sorted_s = sorted(all_s)
        sorted_t = sorted(all_t)
        sorted_m = sorted(all_m)
        sorted_c = sorted(all_c)
        sorted_z = sorted(all_z)

        coord_to_key: dict[tuple[int, ...], tuple[int, int, int, int, int]] = {}
        for t_idx, t_val in enumerate(sorted_t):
            for s_idx, s_val in enumerate(sorted_s):
                for m_idx, m_val in enumerate(sorted_m):
                    for c_idx, c_val in enumerate(sorted_c):
                        for z_idx, z_val in enumerate(sorted_z):
                            coord_to_key[(t_idx, s_idx, m_idx, c_idx, z_idx)] = (s_val, t_val, m_val, z_val, c_val)

        # --- write frames ---
        written = 0
        total_frames = num_t * num_s * num_m * num_c * num_z
        # Signal writing phase with known total
        self.signals.update_progress.emit(-1, total_frames)

        with create_stream(settings) as stream:
            for t_idx in range(num_t):
                for s_idx in range(num_s):
                    for m_idx in range(num_m):
                        for c_idx in range(num_c):
                            for z_idx in range(num_z):
                                key = coord_to_key.get((t_idx, s_idx, m_idx, c_idx, z_idx))
                                if key is not None and key in self.frame_data:
                                    stream.append(self.frame_data[key])
                                else:
                                    stream.skip(frames=1)
                                    logger.warning(
                                        f"Missing frame at "
                                        f"T={t_idx} S={s_idx} "
                                        f"M={m_idx} C={c_idx} "
                                        f"Z={z_idx}"
                                    )
                                written += 1
                                # Per-frame write progress
                                self.signals.update_progress.emit(written, total_frames)

        logger.info(f"OME-ZARR written: {zarr_path}  ({written} frames)")
        # Signal writing complete
        self.signals.update_progress.emit(-2, written)
        self._last_zarr_path = zarr_path
        return zarr_path

    def _open_omezarr_slot(self, zarr_path_str: str) -> None:
        """Open an OME-ZARR dataset in the existing napari viewer.

        This slot runs on the main Qt thread (connected via signal)
        so it is safe to modify the viewer and its layers.

        Delegates to :func:`open_in_napari_viewer` with the
        existing viewer so no new window is created.

        Args:
            zarr_path_str: Filesystem path to the ``.ome.zarr`` dir.
        """
        from napari_zen_streaming.ZEN_omezarr import (
            open_in_napari_viewer,
        )

        try:
            open_in_napari_viewer(Path(zarr_path_str), viewer=self.viewer)
        except Exception as e:
            logger.error(
                f"Failed to open OME-ZARR in viewer: {e}",
                exc_info=True,
            )

    def show(self) -> None:
        """
        Show the viewer window.

        Note:
            In plugin mode, the viewer is already shown by napari.
            This is mainly used in standalone mode.
        """
        self.viewer.show()

    def close(self) -> None:
        """
        Close the viewer and perform cleanup.

        Closes the napari viewer window and logs the action.
        """
        if self.viewer:
            self.viewer.close()
            logger.debug("Viewer closed")
