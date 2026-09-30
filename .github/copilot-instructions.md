# GitHub Copilot Instructions for napari-zenapi-streaming

## Project Overview

**napari-zenapi-streaming** is a napari plugin and standalone CLI tool that streams live microscopy image data from ZEISS ZEN Blue software via the ZEN API (gRPC). It supports two operating modes selectable from the plugin UI:

| Mode | Description |
| --- | --- |
| **Display only** | Streams frames into napari for real-time visualization via an async pipeline with sparse storage and post-acquisition restructure. |
| **OME-ZARR only** | Writes frames directly to OME-ZARR on-the-fly. Bypasses the pipeline and calls the same standalone function as the CLI (`stream_to_omezarr_with_config`). |

A standalone CLI (`ZEN_stream2omezarr.py`) is also available to stream directly to OME-ZARR without napari.

**Key Features:**
- Real-time streaming of microscopy images during acquisition
- Two UI modes: Display only (napari viewer) and OME-ZARR only (direct file write)
- Two acquisition modes: Napari-initiated or ZEN-initiated experiments
- Sparse array storage for efficient memory usage during streaming (Display mode)
- Automatic restructuring from 7D streaming layer to 6D multi-channel layers (Display mode)
- On-the-fly OME-ZARR writing with dedicated gRPC channel (OME-ZARR mode)
- Read-only T/C/Z and Z-spacing controls populated from the selected experiment XML
- Async/await architecture using qasync event loop
- Thread-safe Qt signals for UI updates
- Standalone CLI with two sub-modes (buffered and on-the-fly)

**Authors:** Krijn H. van der Steen (UMC Utrecht), Sebastian Rhode (ZEISS)

## Architecture

### Core Components

1. **main.py** - Application entry point and orchestration
   - `ZENApplication`: Main application class managing Qt/asyncio event loop
   - Handles both embedded (napari plugin) and standalone modes
   - Uses qasync for Qt/asyncio integration

2. **ZEN_config.py** - Configuration management
   - `ZENConfig`: Immutable dataclass for application settings
   - `ImageMetadata`: Dataclass for image dimension metadata (STMZC axes)
   - Reads from environment variables and `.env` file
   - CLI arguments override environment variables

3. **ZEN_init.py** - ZEN API connection and experiment lifecycle
   - `ZENConnection`: Manages gRPC connection to ZEN Blue
   - `ExperimentContext`: Tracks experiment state and metadata
   - Service stubs: ExperimentService, ExperimentStreamingService

4. **ZEN_pipeline.py** - Streaming pipeline orchestration (Display mode only)
   - `StreamingPipeline`: Async reader/processor tasks with queue buffer
   - Monitors ZEN API for new frames
   - Processes frames and updates viewer
   - Context manager for clean startup/shutdown
   - **Not used** in OME-ZARR only mode

5. **ZEN_ui.py** - Napari viewer and UI components
   - `StreamingViewer`: Manages napari viewer and Qt widgets
   - `ViewerSignals`: Thread-safe Qt signals for viewer updates
   - Mode selector dropdown (Display only / OME-ZARR only)
   - Experiment selection dropdown and start button
   - Read-only dimension controls (T, C, Z, Z-spacing) with auto-recalculating total frames
   - Read-only labels for tiles, scenes
   - Output directory, compression, HCS, and pyramid controls; no experiment INI picker
   - Manages 7D→6D layer restructuring after acquisition (Display mode)
   - In OME-ZARR only mode: bypasses pipeline, calls `stream_to_omezarr_with_config` directly

6. **ZEN_utils.py** - Data processing utilities
   - `process_frame()`: Frame processing and metadata extraction
   - Sparse array storage for efficient memory usage
   - Dimension calculation helpers

7. **ZEN_omezarr.py** - OME-ZARR helpers and configuration
   - `ExperimentConfig`: Dataclass with experiment dimensions, output settings
   - `load_experiment_acquisition()`: Exports selected ZEN experiment dimensions and well positions
   - `load_experiment_config()`: Parses standalone CLI experiment INI files
   - `start_experiment()`: Start ZEN experiment via API
   - `build_progress_bar()`: CLI progress bar
   - `open_in_napari_viewer()` / `open_in_ndv_viewer()`: Post-acquisition viewers

8. **ZEN_stream2omezarr.py** - Standalone OME-ZARR streaming
   - `stream_to_omezarr()`: CLI mode — discovers dimensions from stream, buffers all frames, writes at end
   - Configured CLI exports experiment metadata before opening channel subscriptions; INI dimensions are not authoritative
   - `stream_to_omezarr_with_config()`: Config mode — writes on-the-fly with bounded out-of-order buffering. **Used by both CLI and plugin.**
   - Creates its own dedicated gRPC channel (no contention with plugin)
   - Tight `while True:` read loop with frame-count break + inactivity timeout

9. **misc.py** - ZEN API initialization utilities
   - Loads config.ini and establishes gRPC channel to ZEN Blue

## Python Coding Conventions


## Napari-specific instruction

- Please follow napari's guidelines and instruction: [Napari](https://github.com/napari) when writing code for this project.
- Also check this plugin developer guide: [Napari Plugin Developers](https://napari.org/stable/plugins/index.html#plugin-developers)

### Python Instructions

- Write clear and concise comments for each function.
- Ensure functions have descriptive names and include type hints.
- Provide docstrings following PEP 257 conventions.
- Use the `typing` module for type annotations (e.g., `List[str]`, `Dict[str, int]`).
- Break down complex functions into smaller, more manageable functions.

### General Instructions

- Always prioritize readability and clarity.
- For algorithm-related code, include explanations of the approach used.
- Write code with good maintainability practices, including comments on why certain design decisions were made.
- Handle edge cases and write clear exception handling.
- For libraries or external dependencies, mention their usage and purpose in comments.
- Use consistent naming conventions and follow language-specific best practices.
- Write concise, efficient, and idiomatic code that is also easily understandable.

### Code Style and Formatting

- Follow the **PEP 8** style guide for Python.
- Maintain proper indentation (use 4 spaces for each level of indentation).
- Ensure lines do not exceed 79 characters.
- Place function and class docstrings immediately after the `def` or `class` keyword.
- Use blank lines to separate functions, classes, and code blocks where appropriate.

### Edge Cases and Testing

- Always include test cases for critical paths of the application.
- Account for common edge cases like empty inputs, invalid data types, and large datasets.
- Include comments for edge cases and the expected behavior in those cases.
- Write unit tests for functions and document them with docstrings explaining the test cases.

### Example of Proper Documentation

```python
def calculate_area(radius: float) -> float:
    """Calculate the area of a circle given the radius.

    Args:
        radius: The radius of the circle.

    Returns:
        The area of the circle, calculated as π * radius².
    """
    import math
    return math.pi * radius ** 2
```

### Python Style
- **Python 3.11+** minimum (use modern type hints)
- **Line length:** 79 characters (Black formatter setting in this repo)
- **Imports:** Group by standard lib, third-party, local modules
- **Type hints:** Use for function parameters and return values
- **Docstrings:** Google style with Args/Returns/Raises sections
- **Naming:**
  - Classes: `PascalCase`
  - Functions/variables: `snake_case`
  - Constants: `UPPER_SNAKE_CASE`
  - Protected: `_leading_underscore`

### Async/Await Patterns
- Use `async def` for all async functions
- Use `await` for async operations (never `asyncio.run()` inside async functions)
- Use `asyncio.create_task()` for background tasks
- Use `asyncio.sleep()` for non-blocking delays
- Always handle task cancellation with try/except `asyncio.CancelledError`
- Use `async with` for context managers (e.g., `StreamingPipeline`)

### Qt/Threading Guidelines
- **Main thread ONLY:** All napari viewer operations and Qt widget modifications
- **Worker thread:** ZEN API calls, async operations, data processing
- **Communication:** Use Qt signals/slots for thread-safe updates
- **Event loop:** Use qasync.QEventLoop for Qt/asyncio integration
- **Never:** Call Qt methods from worker threads directly

### Memory Management
- Use **sparse array storage** during streaming (dict with tuple keys)
- Convert to dense arrays only when necessary
- Clear old layers before starting new experiments
- Use `del` and gc for large array cleanup

### Logging
- Use Python's `logging` module for new code (not print)
- Use the shared standard-library logging setup in `_logging.py`.
- Log levels: DEBUG for verbose, INFO for milestones, ERROR for failures
- Format: `"%(asctime)s - %(name)s - %(levelname)s - %(message)s"`
- Log files stored in: `logging/zen_streaming.log`
- Include context in log messages (experiment name, frame indices, etc.)

## Key Design Patterns

### 1. Sparse Array Storage
During streaming, frames are stored in a sparse dict to avoid allocating huge 7D arrays:
```python
frame_data: dict[tuple[int, ...], np.ndarray] = {}
# Key: (s_idx, t_idx, m_idx, z_idx, c_idx), Value: 2D array (Y, X)
```

### 2. Two Operating Modes

**Display only** (uses pipeline):
- **Phase 1 (Streaming):** Single 7D layer with shape (N, 1, 1, 1, 1, Y, X)
  - Updates in real-time as frames arrive
  - STMZCYX axis labels
- **Phase 2 (Complete):** Split into multiple 6D channel layers
  - One layer per channel
  - Shape: (S, T, M, Z, Y, X)
  - Named: "Channel_0", "Channel_1", etc.

**OME-ZARR only** (bypasses pipeline):
- Calls `stream_to_omezarr_with_config(ecfg)` directly
- Same function used by the standalone CLI
- Creates a dedicated gRPC channel (no contention)
- Reads frames in tight `while True:` loop
- Writes to OME-ZARR on-the-fly via `ome-writers`
- Breaks on `frame_count >= total_expected` or inactivity timeout
- UI supplies experiment name, output directory, and writer options; exported
   XML supplies T/C/Z/M/S, Z spacing, and scene-to-well positions before streaming

### 3. Experiment Lifecycle
```python
# Napari-started (has experiment_id)
prepare_experiment() → arm FrameData.experiment_id filter
   → start_loaded_experiment() → drain trailing frames → restructure

# ZEN-started (no experiment_id)
detect first frame → monitor via timeout → stop after no frames for X seconds

# OME-ZARR only mode
stream_to_omezarr_with_config(ecfg) → own gRPC channel → frame-count break
```

Display mode deliberately keeps one `monitor_all_experiments()` transport
instead of switching to `monitor_experiment()`: ZEN may close the targeted
stream when status becomes finished before every pixel payload is delivered.
For Napari-started runs, `StreamingPipeline` filters the global responses by
`FrameData.experiment_id`. The ID filter must be armed after loading and before
starting the experiment, retained during trailing-frame drain, and cleared
only after final restructure. Never open both monitor RPCs concurrently; ZEN
distributes frames between active consumers.

### 4. Configuration Precedence
```
Standalone app: CLI args > environment / .env > defaults
Napari widget: package-local .env (loaded with override=True) > environment > defaults
Experiment INI: CLI overrides > INI settings; exported XML replaces INI dimensions
```

### 5. Error Handling
- Use try/except with specific exception types
- Log errors with `exc_info=True` for tracebacks
- Clean up resources in finally blocks
- Graceful degradation when possible

## Dependencies

### Core
- **napari[all]** - Image viewer (Qt-based)
- **qasync** - Qt/asyncio event loop integration
- **numpy** - Array operations
- **grpclib** - gRPC client for ZEN API
- **ome-writers** - OME-ZARR streaming writer (AcquisitionSettings, create_stream)
- **python-dotenv** - Environment variable loading
- **pydantic** - Data validation
- **logging** - Standard-library logging with terminal and rotating-file handlers

### ZEN API
- **zen_api** - ZEISS ZEN API client (gRPC stubs)
   - The Conda environment manifest installs the `zen_api-2026.05.1` wheel
  - Always use raw.githubusercontent.com URLs, not github.com/blob

### Development
- **pytest** - Testing
- **black** - Code formatting
- **mypy** - Type checking

## Common Tasks

### Adding a New Configuration Parameter
1. Add field to `ZENConfig` dataclass in `ZEN_config.py`
2. Update `from_env()` classmethod to read from environment
3. Update CLI argument parser in `main.py`
4. Update `.env` example in README
5. Document in configuration table

### Adding a New Layer Type
1. Update `StreamingViewer._create_layer_slot()` and/or `StreamingViewer.perform_restructure()`
2. Handle new dimensions in sparse storage keys and `_build_channel_6d_array()`
3. Update metadata extraction in `ZEN_utils.process_frame()`
4. Test with an appropriate ZEN experiment

### Adding a New Editable Dimension to the UI
1. Add a `QSpinBox` or `QDoubleSpinBox` to `StreamingViewer.__init__()` in the `dim_panel` form
2. Connect its `valueChanged` signal to `_on_dim_changed()` if it affects total frame count
3. Populate it from selected ZEN experiment metadata in `_apply_experiment_metadata()`
4. Pass any writer setting through `_create_plugin_omezarr_config()`

### Debugging Streaming Issues
1. Check `logging/zen_streaming.log` for errors
2. Verify ZEN Blue is running and experiment is active
3. Test gRPC connection with `ZENConnection.get_experiments()`
4. For Display mode: Add DEBUG logging to `StreamingPipeline._read_frames()`
5. For OME-ZARR mode: Run `stream_to_omezarr_with_config` from CLI first to isolate plugin vs. gRPC issues
6. Check frame metadata consistency
7. Compare received frame_count vs total_expected in log output

### Performance Optimization
- Use `numpy.ascontiguousarray()` for memory layout
- Minimize layer updates (batch when possible)
- Use `viewer.layers[idx].data` assignment, not methods
- Profile with `cProfile` or `line_profiler`
- Monitor memory with `tracemalloc`

## File Structure

```
napari-zenapi_streaming/
├── src/napari_zen_streaming/      # Main package
│   ├── main.py                    # Entry point
│   ├── ZEN_config.py              # Configuration
│   ├── ZEN_init.py                # Connection/experiment
│   ├── ZEN_pipeline.py            # Streaming pipeline (Display mode)
│   ├── ZEN_ui.py                  # Viewer/UI, mode switching
│   ├── ZEN_utils.py               # Processing utilities
│   ├── ZEN_omezarr.py             # OME-ZARR helpers, ExperimentConfig
│   ├── ZEN_stream2omezarr.py      # OME-ZARR streaming (CLI + plugin)
│   ├── _widget.py                 # Plugin widget entrypoint
│   ├── misc.py                    # ZEN API init
│   ├── napari.yaml                # Plugin manifest
│   ├── .env                       # Environment variable overrides
│   └── logging/                   # Log files
├── docs/                          # Architecture diagrams
├── python_env/                    # Conda environment files
├── snippets/                      # Standalone code snippets
├── config.ini                     # ZEN API connection config
├── experiment_streaming_config.ini # Experiment config for OME-ZARR
├── pyproject.toml                 # Package metadata
└── ReadMe.md                      # Documentation
```

## Testing Guidelines

### Unit Tests
- Test configuration parsing and validation
- Test metadata extraction from frames
- Mock ZEN API responses
- Test sparse→dense array conversion

### Integration Tests
- Test full streaming pipeline with mock frames
- Test experiment lifecycle (start/stop)
- Test layer creation and updates
- Requires pytest-asyncio for async tests

### Manual Testing Checklist
- [ ] Display mode: Napari-started experiment streams correctly
- [ ] Display mode: ZEN-started experiment detected and streams
- [ ] Display mode: Multiple channels restructure properly
- [ ] Display mode: Layer cleanup when starting new experiment
- [ ] OME-ZARR mode: All frames received (frame_count == total_expected)
- [ ] OME-ZARR mode: XML-derived T/C/Z/M/S, Z spacing, and well positions applied before streaming
- [ ] OME-ZARR mode: Output directory and compression from UI respected
- [ ] OME-ZARR mode: Experiment name from dropdown, not INI
- [ ] CLI: `stream_to_omezarr_with_config` receives all frames (identical to plugin)
- [ ] CLI config mode: export channels and scene-to-well positions before opening pixel streams
- [ ] Graceful shutdown on viewer close
- [ ] Error messages are user-friendly

## Common Pitfalls to Avoid

1. **Don't** call Qt methods from worker threads → Use signals
2. **Don't** use `asyncio.run()` inside async functions → Use `await`
3. **Don't** forget to cancel tasks on cleanup → Leads to warnings
4. **Don't** modify viewer layers outside main thread → Race conditions
5. **Don't** allocate full 7D arrays during streaming → Memory explosion
6. **Don't** use github.com/blob URLs for wheel files → Use raw.githubusercontent.com
7. **Don't** run scripts with full paths → Use `python -m module_name`
8. **Don't** forget to create logging directory → App will crash
9. **Don't** use the pipeline/shared gRPC channel for OME-ZARR mode → Frame loss due to contention. Always use `stream_to_omezarr_with_config` which creates its own dedicated channel.
10. **Don't** rely on experiment-status "finished" to stop reading frames → ZEN sends the finished status before all pixel data is delivered via gRPC. Use frame-count completion instead.

## ZEN API Specifics

### Service Stubs
```python
ExperimentServiceStub            # Load, start, stop experiments
ExperimentStreamingServiceStub   # Monitor frames, get metadata
```

### Frame Monitoring
```python
# Monitor all experiments (used in both modes)
MonitorAllExperimentsRequest(
    channel_index=channel_index,
    enable_raw_data=False,
)

# Monitor specific experiment (alternative, less common)
MonitorExperimentRequest(experiment_id=exp_id)
```

### Frame Data Access (new zen_api package)
The `zen_api` package uses different field names than the legacy stubs:
```python
fd = response.frame_data
fp = fd.frame_position
t, z, c, m, s = fp.t, fp.z, fp.c, fp.m, fp.s
frame = np.frombuffer(fd.pixel_data.raw_data, dtype=dtype)
    .reshape((fd.frame_size.height, fd.frame_size.width))
sx = fd.scaling.x * 1e6  # m → µm
```

### Important: Frame Ordering
ZEN sends frames grouped by channel: all C=0 frames first (across all T/Z),
then all C=1 frames, etc.  This means the experiment-status "finished" signal
arrives before all pixel data has been delivered.  Use frame-count completion
(`frame_count >= total_expected`) instead of status-based detection for
OME-ZARR mode.

### Metadata Fields (legacy stubs / Display mode)
- `image_index` - Frame number in acquisition
- `s_index`, `t_index`, `m_index`, `z_index`, `c_index` - STMZC indices
- `width`, `height` - Image dimensions
- `pixel_type` - Data type (e.g., "Gray16")

## Napari Plugin Integration

### Entry Point
```toml
[project.entry-points."napari.manifest"]
napari-zenapi-streaming = "napari_zen_streaming:napari.yaml"
```

### Plugin Manifest (napari.yaml)
Defines widgets and contributions to napari

### Widget Naming
- `_widget.py` - Main plugin widget

## Environment Setup

### Installation
```bash
# Create conda environment
conda env create -f python_env/env_zenapi_napari.yml
conda activate zenapi-napari

# Install package in development mode
pip install -e .

# The Conda environment above already includes the ZEN API wheel.
```

### Running
```bash
# As module (preferred)
python -m napari_zen_streaming.main

# With arguments
python -m napari_zen_streaming.main --ui-start true --exp-name MyExperiment

# As napari plugin
napari  # Then use Plugins menu
```

## Code Generation Preferences

When generating code:
1. **Include type hints** for all parameters and returns
2. **Add docstrings** with Args/Returns/Raises
3. **Use async/await** for I/O operations
4. **Handle errors** with specific exceptions
5. **Add logging** at appropriate levels
6. **Follow 79-char line limit**
7. **Use dataclasses** for configuration objects
8. **Prefer composition** over inheritance
9. **Keep functions focused** (single responsibility)
10. **Write defensive code** (validate inputs)

## References

- [Napari Documentation](https://napari.org/stable/)
- [qasync Documentation](https://github.com/CabbageDevelopment/qasync)
- [ZEISS ZEN API Documentation](https://github.com/zeiss-microscopy/OAD)
- [gRPC Python](https://grpc.io/docs/languages/python/)
- [PyQt5 Documentation](https://www.riverbankcomputing.com/static/Docs/PyQt5/)
