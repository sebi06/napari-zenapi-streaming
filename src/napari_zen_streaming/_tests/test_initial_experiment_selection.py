"""Tests for initial experiment selection metadata loading."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from napari_zen_streaming import main
from napari_zen_streaming import ZEN_stream2omezarr
from napari_zen_streaming.ZEN_omezarr import create_plugin_experiment_config
from napari_zen_streaming.ZEN_ui import StreamingViewer


class _ExperimentDropdown:
    """Minimal combo-box stand-in for experiment population tests."""

    def __init__(self) -> None:
        self.items: list[str] = []
        self.current_index = -1
        self.signals_blocked = False

    def blockSignals(self, blocked: bool) -> bool:
        """Set signal state and return its previous value."""
        previous = self.signals_blocked
        self.signals_blocked = blocked
        return previous

    def clear(self) -> None:
        """Remove all experiment names."""
        self.items.clear()
        self.current_index = -1

    def addItems(self, items: list[str]) -> None:
        """Add experiment names and select the first entry."""
        self.items.extend(items)
        if items:
            self.current_index = 0

    def findText(self, text: str) -> int:
        """Return the matching item index or -1."""
        try:
            return self.items.index(text)
        except ValueError:
            return -1

    def setCurrentIndex(self, index: int) -> None:
        """Select an experiment by index."""
        self.current_index = index

    def currentText(self) -> str:
        """Return the selected experiment name."""
        if self.current_index < 0:
            return ""
        return self.items[self.current_index]


class _ValueControl:
    """Minimal value widget used by metadata application tests."""

    def __init__(self) -> None:
        self.current_value: float = 1

    def setValue(self, value: float) -> None:
        """Store a value assigned by the viewer."""
        self.current_value = value

    def value(self) -> float:
        """Return the stored value."""
        return self.current_value


class _ChannelCombo:
    """Minimal channel selector for testing mode-dependent choices."""

    def __init__(self) -> None:
        self.items: list[tuple[str, int | None]] = [("All channels", None)]
        self.index = 0
        self.signals_blocked = False

    def blockSignals(self, blocked: bool) -> bool:
        """Set signal state and return its previous value."""
        previous = self.signals_blocked
        self.signals_blocked = blocked
        return previous

    def clear(self) -> None:
        """Remove all channel choices."""
        self.items.clear()
        self.index = -1

    def addItem(self, label: str, channel_index: int | None) -> None:
        """Append one labelled channel choice."""
        self.items.append((label, channel_index))

    def findData(self, channel_index: int | None) -> int:
        """Find a channel by its ZEN index."""
        return next((index for index, (_, value) in enumerate(self.items) if value == channel_index), -1)

    def setCurrentIndex(self, index: int) -> None:
        """Select one channel choice."""
        self.index = index

    def currentIndex(self) -> int:
        """Return the selected choice index."""
        return self.index

    def currentData(self) -> int | None:
        """Return the selected ZEN channel index."""
        return self.items[self.index][1]


def test_population_refreshes_configured_initial_experiment() -> None:
    """Initial population loads dimensions for the final selection once."""
    viewer = StreamingViewer.__new__(StreamingViewer)
    viewer.dropdown = _ExperimentDropdown()
    viewer.config = SimpleNamespace(exp_name="Z Stack")
    refreshed: list[str] = []
    viewer.refresh_selected_experiment_metadata = lambda: refreshed.append(viewer.dropdown.currentText())

    viewer.populate_experiments(["Time Series", "Z Stack"])

    assert viewer.dropdown.items == ["Time Series", "Z Stack"]
    assert viewer.dropdown.currentText() == "Z Stack"
    assert refreshed == ["Z Stack"]
    assert not viewer.dropdown.signals_blocked


def test_refresh_reloads_metadata_without_changing_selection() -> None:
    """The refresh action requests fresh XML for the selected experiment."""
    viewer = StreamingViewer.__new__(StreamingViewer)
    viewer.dropdown = SimpleNamespace(currentText=lambda: "Plate Scan")
    viewer.dim_panel = SimpleNamespace(setVisible=lambda visible: None)
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: 2)
    viewer.connection = object()
    viewer._selected_experiment_metadata = object()
    viewer._expected_total_frames = 10
    viewer._metadata_request_id = 0
    refreshed = SimpleNamespace(experiment_name="Plate Scan", channels=5)
    viewer._load_selected_experiment_metadata = AsyncMock(return_value=refreshed)
    received: list[object] = []
    viewer.signals = SimpleNamespace(experiment_metadata_loaded=SimpleNamespace(emit=received.append))
    viewer.safe_execution = lambda callback: asyncio.run(callback())

    viewer.refresh_selected_experiment_metadata()

    viewer._load_selected_experiment_metadata.assert_awaited_once_with("Plate Scan")
    assert received == [refreshed]
    assert viewer._selected_experiment_metadata is None
    assert viewer._expected_total_frames is None


def test_start_reload_uses_fresh_metadata_and_ignores_outdated_result() -> None:
    """Every start fetches XML, but superseded results must not update the UI."""
    viewer = StreamingViewer.__new__(StreamingViewer)
    viewer._metadata_request_id = 1
    viewer._selected_experiment_metadata = SimpleNamespace(experiment_name="Plate Scan", channels=2)
    updated = SimpleNamespace(experiment_name="Plate Scan", channels=5)
    viewer._load_selected_experiment_metadata = AsyncMock(return_value=updated)
    published: list[object] = []
    viewer.signals = SimpleNamespace(experiment_metadata_loaded=SimpleNamespace(emit=published.append))

    assert asyncio.run(viewer._reload_metadata_for_start("Plate Scan", 1)) is updated
    viewer._metadata_request_id = 2
    assert asyncio.run(viewer._reload_metadata_for_start("Plate Scan", 1)) is updated

    assert viewer._load_selected_experiment_metadata.await_count == 2
    assert published == [updated]


@pytest.mark.parametrize("mode", [1, 2])
def test_start_refreshes_selected_experiment_before_acquisition(mode: int, monkeypatch) -> None:
    """Both napari start paths fetch current XML before acquiring or writing."""
    viewer = StreamingViewer.__new__(StreamingViewer)
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: mode)
    viewer.dropdown = SimpleNamespace(currentText=lambda: "Plate Scan")
    viewer.dim_panel = SimpleNamespace(setVisible=lambda visible: None)
    viewer.connection = object()
    viewer._selected_experiment_metadata = SimpleNamespace(experiment_name="Plate Scan", channels=2)
    viewer._expected_total_frames = 2
    viewer._metadata_request_id = 0
    viewer.frame_data = {}
    viewer.frame_metadata_by_key = {}
    viewer.frame_keys = []
    viewer.unique_channels = set()
    viewer.restructure_task = None
    viewer.status_monitor_task = None
    viewer._clear_layers = lambda: None
    viewer._close_progress_bar = lambda: None
    viewer._reset_zarr_state = lambda: None
    viewer._freeze_live_preview_option = lambda: None
    viewer.safe_execution = lambda callback: asyncio.run(callback())
    viewer._monitor_experiment_status = AsyncMock()
    published: list[object] = []
    opened: list[str] = []
    viewer.signals = SimpleNamespace(
        show_start_button=SimpleNamespace(emit=lambda enabled: None),
        show_stop_button=SimpleNamespace(emit=lambda visible: None),
        experiment_metadata_loaded=SimpleNamespace(emit=published.append),
        update_progress=SimpleNamespace(emit=lambda current, total: None),
        open_omezarr=SimpleNamespace(emit=opened.append),
    )
    fresh = SimpleNamespace(
        experiment_name="Plate Scan",
        time_points=3,
        channels=5,
        z_planes=2,
        z_spacing=0.5,
        tiles=1,
        scenes=1,
        positions=[],
    )
    events: list[str] = []

    async def load_metadata(experiment_name: str) -> object:
        events.append("reload")
        return fresh

    viewer._load_selected_experiment_metadata = load_metadata
    pipeline = SimpleNamespace(
        start_targeted_experiment=AsyncMock(side_effect=lambda name: events.append("start") or "exp-id"),
        suspend_reader=AsyncMock(),
        resume_reader=AsyncMock(),
    )
    viewer.app = SimpleNamespace(pipeline=pipeline)
    viewer._create_plugin_omezarr_config = lambda name: create_plugin_experiment_config(
        experiment_name=name,
        output_dir=".",
        dtype="Gray16",
        compression=None,
        zenapi_config="config.ini",
        start_from_script=True,
        use_hcs_layout=False,
        keep_source_tiles=False,
    )

    async def write_zarr(ecfg, **kwargs):
        events.append("write")
        kwargs["on_experiment_started"]("experiment-1")
        assert viewer.current_experiment_id == "experiment-1"
        return "result.zarr"

    writer = AsyncMock(side_effect=write_zarr)
    monkeypatch.setattr(ZEN_stream2omezarr, "stream_to_omezarr_with_config", writer)

    viewer.on_start_clicked()

    assert published == [fresh]
    assert events == (["reload", "start"] if mode == 1 else ["reload", "write"])
    assert opened == (["result.zarr"] if mode == 2 else [])
    if mode == 2:
        assert writer.await_args.args[0].channels == 5
        assert writer.await_args.args[0].time_points == 3
        assert callable(writer.await_args.kwargs["on_experiment_started"])
        assert viewer.current_experiment_id is None


def test_stop_omezarr_uses_started_experiment_id() -> None:
    """Stop targets the writer's experiment and waits to re-enable Start."""
    viewer = StreamingViewer.__new__(StreamingViewer)
    viewer.current_experiment_id = "experiment-1"
    viewer._standalone_zarr_active = True
    viewer.status_monitor_task = None
    viewer._close_progress_bar = lambda: None
    viewer.connection = SimpleNamespace(stop_experiment=AsyncMock())
    viewer.safe_execution = lambda callback: asyncio.run(callback())
    start_states = []
    viewer.signals = SimpleNamespace(
        show_stop_button=SimpleNamespace(emit=lambda enabled: None),
        show_start_button=SimpleNamespace(emit=start_states.append),
    )

    viewer.on_stop_clicked()

    viewer.connection.stop_experiment.assert_awaited_once_with("experiment-1")
    assert viewer.current_experiment_id is None
    assert start_states == []


def test_stop_omezarr_before_start_keeps_stop_available() -> None:
    """A pending auto-start cannot be stopped until ZEN returns its ID."""
    viewer = StreamingViewer.__new__(StreamingViewer)
    viewer.current_experiment_id = None
    viewer._standalone_zarr_active = True
    viewer.connection = SimpleNamespace(stop_experiment=AsyncMock())
    viewer.signals = SimpleNamespace(show_stop_button=SimpleNamespace(emit=lambda enabled: None))

    viewer.on_stop_clicked()

    viewer.connection.stop_experiment.assert_not_awaited()


def test_selected_metadata_updates_derived_total_frames() -> None:
    """Applying initial metadata derives total frames from all dimensions."""
    viewer = StreamingViewer.__new__(StreamingViewer)
    viewer.dropdown = SimpleNamespace(currentText=lambda: "Plate Scan")
    viewer.spin_t = _ValueControl()
    viewer.spin_c = _ValueControl()
    viewer.spin_z = _ValueControl()
    viewer.spin_z_spacing = _ValueControl()
    viewer.label_tiles = _ValueControl()
    viewer.label_scenes = _ValueControl()
    viewer.label_total = SimpleNamespace(setText=lambda text: totals.append(text))
    visibility: list[bool] = []
    viewer.dim_panel = SimpleNamespace(setVisible=lambda visible: visibility.append(visible))
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: 1)
    viewer._refresh_channel_filter = lambda: None
    viewer.chk_hcs_layout = SimpleNamespace(setChecked=lambda _checked: None)
    viewer._update_hcs_layout_option = lambda: None
    viewer._selected_experiment_metadata = None
    viewer._expected_total_frames = None
    totals: list[str] = []
    metadata = SimpleNamespace(
        experiment_name="Plate Scan",
        time_points=2,
        channels=3,
        z_planes=4,
        z_spacing=1.5,
        tiles=5,
        scenes=6,
    )

    viewer._apply_experiment_metadata(metadata)

    assert totals == ["720"]
    assert viewer._expected_total_frames == 720
    assert viewer._selected_experiment_metadata is metadata
    assert visibility == [True]


def test_mode_switch_keeps_dimensions_and_hides_writer_options() -> None:
    """Both modes show loaded dimensions; only ZARR shows writer settings."""
    viewer = StreamingViewer.__new__(StreamingViewer)
    visibility: dict[str, list[bool]] = {}
    for name in (
        "experiment_label",
        "dropdown",
        "refresh_experiment_button",
        "start_button",
        "auto_trigger_row",
        "channel_filter_row",
        "chk_live_latest",
        "omezarr_dir_row",
        "omezarr_comp_row",
        "dim_panel",
        "omezarr_options_panel",
    ):
        visibility[name] = []
        setattr(
            viewer,
            name,
            SimpleNamespace(setVisible=lambda visible, name=name: visibility[name].append(visible)),
        )
    viewer._selected_experiment_metadata = object()
    viewer._update_hcs_layout_option = lambda: None
    viewer._refresh_channel_filter = lambda: None
    viewer.start_button.isEnabled = lambda: False

    viewer._on_mode_changed(0)
    viewer._on_mode_changed(1)
    viewer._on_mode_changed(2)

    assert visibility["dim_panel"] == [False, True, True]
    assert visibility["omezarr_options_panel"] == [False, False, True]
    assert visibility["refresh_experiment_button"] == [False, True, True]
    assert visibility["start_button"] == [False, True, True]


def test_channel_filter_uses_active_display_workflow() -> None:
    """Watch accepts 1-16; selected experiments accept only their channels."""
    viewer = StreamingViewer.__new__(StreamingViewer)
    mode = [1]
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: mode[0])
    viewer.dropdown = SimpleNamespace(currentText=lambda: "Plate Scan")
    viewer.channel_filter_combo = _ChannelCombo()
    viewer._selected_experiment_metadata = SimpleNamespace(experiment_name="Plate Scan", channels=3)
    viewer._pending_channel_index = 2
    viewer._effective_channel_index = 2
    viewer.app = SimpleNamespace(pipeline=None)

    viewer._refresh_channel_filter()
    assert viewer.channel_filter_combo.items == [("All channels", None), ("1", 0), ("2", 1), ("3", 2)]
    assert viewer.channel_filter_combo.currentData() == 2

    viewer._selected_experiment_metadata = None
    viewer._refresh_channel_filter()
    assert viewer.channel_filter_combo.items == [("All channels", None)]
    assert viewer._pending_channel_index == 2
    viewer._selected_experiment_metadata = SimpleNamespace(experiment_name="Plate Scan", channels=3)
    viewer._refresh_channel_filter()
    assert viewer.channel_filter_combo.currentData() == 2

    mode[0] = 0
    viewer._refresh_channel_filter()
    assert len(viewer.channel_filter_combo.items) == 17
    assert viewer.channel_filter_combo.items[-1] == ("16", 15)
    viewer.channel_filter_combo.setCurrentIndex(5)
    viewer._on_channel_filter_changed(5)
    assert viewer._effective_channel_index == 4

    mode[0] = 1
    viewer._refresh_channel_filter()
    assert len(viewer.channel_filter_combo.items) == 4
    assert viewer.channel_filter_combo.currentData() is None
    assert viewer._effective_channel_index is None

    mode[0] = 0
    assert viewer._expected_display_frame_count() == 0


def test_plugin_setup_waits_for_explicit_start(monkeypatch) -> None:
    """Connecting to ZEN must not start the configured experiment."""
    connection = SimpleNamespace(get_experiments=lambda: asyncio.sleep(0, result=["Plate Scan"]))
    context = SimpleNamespace(initialize=lambda: (_ for _ in ()).throw(AssertionError("started")))
    monkeypatch.setattr(main, "ZENConnection", lambda config: connection)
    monkeypatch.setattr(main, "ExperimentContext", lambda connection, config: context)
    app = main.ZENApplication.__new__(main.ZENApplication)
    app.config = SimpleNamespace()

    assert asyncio.run(app.setup_connection()) == ["Plate Scan"]


def test_watch_mode_does_not_start_selected_experiment() -> None:
    """Passive ZEN watching never uses the selected-setup start action."""
    viewer = StreamingViewer.__new__(StreamingViewer)
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: 0)

    assert viewer.on_start_clicked() is None
