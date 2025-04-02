# Background Magic for Jupyter

A Jupyter/IPython magic extension (`%%background`) that allows running cell code in a background process.

## Installation

```bash
# Navigate to the project root directory
pip install -e .
# or
pip install .
```

## Usage

In a Jupyter Notebook or IPython session:

1.  Load the extension:
    ```python
    %load_ext background_magic
    ```

2.  Use the magic:
    ```python
    %%background
    import time # <--- IMPORTANT: Import modules needed WITHIN the cell
    import numpy as np

    print("Starting background task...")
    # Access global variables (copied at start)
    # print(f"Accessing global_var: {my_global_variable}") 
    time.sleep(5)
    print(f"Calculation result: {np.random.rand()}")
    print("Background task finished.")
    ```

    You should be able to execute other cells while the background task is running. Output (stdout, stderr, plots) will be directed back to the original cell.

## Key Features & Limitations

*   **Background Execution:** Runs the cell code in a separate process using `multiprocessing`.
*   **Global Variables:** Accesses a *copy* of the global variables (excluding modules, IPython internals) from the main kernel at the time the cell starts. Modifications to these variables in the background do not affect the main kernel.
*   **Output Streaming:** Streams `stdout`, `stderr`, and rich display outputs (like Matplotlib plots) back to the original cell output area.
*   **Isolation:** Each background task runs independently.
*   **Single Instance Per Cell:** Running the same cell with `%%background` again while a previous instance is still running will stop the previous instance before starting the new one.
*   **IMPORTANT - Module Imports:** Modules **must be imported within the `%%background` cell**. Imports from the main notebook scope are not automatically available due to process isolation and serialization limitations.

## TODO

-   [ ] Add more robust tests, especially for display output capture.
-   [ ] Potentially add arguments to the magic (e.g., `%%background --name my_task`).

-   [ ] Implement actual background process execution.
-   [ ] Capture stdout/stderr from the background process.
-   [ ] Capture rich display outputs (plots, dataframes, etc.).
-   [ ] Stream outputs back to the original cell output area.
-   [ ] Manage multiple concurrent background tasks.
-   [ ] Add tests. 