"""Tests for initial experiment selection metadata loading."""

from types import SimpleNamespace

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
    viewer.dim_panel = SimpleNamespace(setVisible=lambda _visible: None)
    viewer.mode_combo = SimpleNamespace(currentIndex=lambda: 0)
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
