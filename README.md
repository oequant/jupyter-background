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
    result = np.random.rand()  # This variable will be returned to the main process
    print(f"Calculation result: {result}")
    print("Background task finished.")
    ```

    You should be able to execute other cells while the background task is running. Output (stdout, stderr, plots) will be directed back to the original cell.

3.  Using namespaces to isolate variables:
    ```python
    %%background analysis1
    import numpy as np
    
    data = np.random.rand(100)
    mean = data.mean()
    std = data.std()
    print(f"Analysis 1 - Mean: {mean}, Std: {std}")
    ```
    
    Then in another cell with the same namespace:
    ```python
    %%background analysis1
    # Variables from previous 'analysis1' cells are available
    print(f"Data from prior cell has shape {data.shape}")
    median = np.median(data)
    print(f"Median: {median}")
    ```

## Key Features

*   **Background Execution:** Runs the cell code in a separate process using `multiprocessing`.
*   **Global Variables:** Accesses a *copy* of the global variables (excluding modules, IPython internals) from the main kernel at the time the cell starts.
*   **Variable Return:** Variables defined or modified in the background process are automatically returned to either:
    * The main kernel's global namespace (when using `%%background` without a name)
    * A specific namespace when using `%%background space_name` syntax
*   **Smart Variable Tracking:** Only variables that are new or modified in the background process are returned.
*   **Unpicklable Object Handling:** Safely skips unpicklable objects (like modules or complex functions) with appropriate warnings.
*   **Namespaces:** Using `%%background space_name` allows isolating variables to specific contexts
*   **Output Streaming:** Streams `stdout`, `stderr`, and rich display outputs (like Matplotlib plots) back to the original cell output area.
*   **Isolation:** Each background task runs independently.
*   **Single Instance Per Cell:** Running the same cell with `%%background` again while a previous instance is still running will stop the previous instance before starting the new one.
*   **IMPORTANT - Module Imports:** Modules **must be imported within the `%%background` cell**. Imports from the main notebook scope are not automatically available due to process isolation and serialization limitations.

## TODO

-   [x] Add variable return from background processes
-   [x] Add namespace support for isolating variables
-   [x] Handle unpicklable objects gracefully
-   [ ] Add more robust tests, especially for display output capture.
-   [ ] Potentially add more arguments to the magic.

-   [ ] Implement actual background process execution.
-   [ ] Capture stdout/stderr from the background process.
-   [ ] Capture rich display outputs (plots, dataframes, etc.).
-   [ ] Stream outputs back to the original cell output area.
-   [ ] Manage multiple concurrent background tasks.
-   [ ] Add tests. 