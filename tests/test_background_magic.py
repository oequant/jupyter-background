import pytest
from IPython.testing.globalipapp import get_ipython
# Try importing the shell class directly
from IPython.terminal.interactiveshell import TerminalInteractiveShell
import time
import pandas as pd
import os
import tempfile
import json
import cloudpickle
from queue import Queue # For capturing output in test
from unittest.mock import patch, MagicMock # Add MagicMock
import sys
from io import StringIO

# Fixture to get a fresh IPython instance for each test
@pytest.fixture(scope='function')
def ip():
    """Get a fresh IPython shell instance."""
    # Use instance() method for potentially better reliability in tests
    shell = TerminalInteractiveShell.instance()
    # Ensure user_ns is clean if reusing instances across tests (though scope='function' should handle this)
    shell.reset(new_session=True)
    shell.run_line_magic('load_ext', 'background_magic')
    yield shell
    # Cleanup after test
    try:
        shell.run_line_magic('unload_ext', 'background_magic')
    except KeyError:
        pass
    shell.reset(new_session=True)
    # It might be necessary to properly close/destroy the instance if instance() is used
    # TerminalInteractiveShell.clear_instance()

# Placeholder test
def test_placeholder():
    assert True 

def test_background_accesses_globals(ip):
    """Test if %%background can access various global types."""
    # Define globals in the IPython namespace
    ip.user_ns['test_str'] = "hello"
    ip.user_ns['test_int'] = 123
    ip.user_ns['test_list'] = [1, 'a']
    ip.user_ns['test_dict'] = {'x': 1}
    test_df_original = pd.DataFrame({'a': [1, 2]})
    ip.user_ns['test_df'] = test_df_original
    ip.user_ns['test_func'] = lambda x: x + 1

    # Temp file for pickled results
    with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.pkl') as tmpfile:
        result_filepath = tmpfile.name

    # Code to run in the background
    # Access globals and pickle results (except status/error)
    background_code = f"""
import time
import cloudpickle
import json # For status/error structure
import pandas as pd # Need pandas inside too

results = {{}}
try:
    # Store results directly
    results['str_val'] = test_str
    results['int_val'] = test_int
    results['list_val'] = test_list
    results['dict_val'] = test_dict
    results['df_val'] = test_df # Store DataFrame object
    results['func_val'] = test_func(10)
    results['status'] = 'success'
except Exception as e:
    results['status'] = 'error'
    results['error'] = str(e)

# Write results to the temp file using cloudpickle
filepath = r'{result_filepath}'
try:
    with open(filepath, 'wb') as f:
        cloudpickle.dump(results, f)
except Exception as e:
    # If pickling fails, try to write basic error status
    try:
        with open(filepath, 'w') as f_err: # Open as text for JSON
             json.dump({{'status':'pickle_error', 'error': str(e)}}, f_err)
    except Exception:
        import sys
        print(f"Failed to write test results: {{e}}", file=sys.stderr)

time.sleep(0.5)
"""

    ip.run_cell(f"%%background\n{background_code}")

    max_wait_secs = 10
    start_wait = time.time()
    results_data = None
    while time.time() - start_wait < max_wait_secs:
        try:
            if os.path.exists(result_filepath) and os.path.getsize(result_filepath) > 0:
                # Load pickled results
                with open(result_filepath, 'rb') as f:
                    # Check if it's the fallback JSON error message first
                    if result_filepath.endswith('.pkl'): # Should always be true here
                        try:
                            # Read first few bytes to see if it looks like JSON
                            start_bytes = f.read(2)
                            f.seek(0)
                            if start_bytes == b'{{":': # Simple check for json start
                                # Try loading as json error
                                results_data = json.load(f)
                            else:
                                results_data = cloudpickle.load(f)
                        except Exception as load_err:
                             print(f"Failed to load results file: {load_err}")
                             # Treat as error for assertion
                             results_data = {'status': 'load_error', 'error': str(load_err)}
                    else: # Should not happen with suffix='.pkl'
                         results_data = {'status': 'wrong_suffix'}
                break
        except FileNotFoundError:
            pass
        time.sleep(0.5)

    if os.path.exists(result_filepath):
        os.remove(result_filepath)

    assert results_data is not None, "Test results file was not created or was empty."
    assert results_data.get('status') == 'success', f"Background task failed: {results_data.get('error')}"
    assert results_data.get('str_val') == "hello"
    assert results_data.get('int_val') == 123
    assert results_data.get('list_val') == [1, 'a']
    assert results_data.get('dict_val') == {'x': 1}
    # Compare DataFrame using pandas equality check
    pd.testing.assert_frame_equal(results_data.get('df_val'), test_df_original)
    assert results_data.get('func_val') == 11

# Revived test: Checks if modules imported externally are usable internally
def test_background_uses_imported_modules(ip):
    """Test if %%background can use modules imported in the main scope."""
    # Import modules in the main IPython namespace
    ip.run_cell("import pandas as pd_alias")
    ip.run_cell("import numpy as np_alias")
    ip.run_cell("import time as time_alias") # Add time module

    # Check if aliases exist in user_ns (they should)
    assert 'pd_alias' in ip.user_ns
    assert 'np_alias' in ip.user_ns
    assert 'time_alias' in ip.user_ns

    # Temp file for results
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as tmpfile:
        result_filepath = tmpfile.name

    # Code to run in the background
    # It attempts to use the aliases defined outside
    # NO internal imports are used here.
    background_code = f"""
import json # Still need json for writing result file

results = {{}}
try:
    # Attempt to use the aliases imported in the parent scope
    df = pd_alias.DataFrame({{'col': [1, 2]}})
    arr = np_alias.array([10, 20])
    time_alias.sleep(0.01) # Use time module
    results['df_shape'] = df.shape
    results['arr_sum'] = int(arr.sum())
    results['time_slept'] = True
    results['status'] = 'success'
except NameError as e:
    # Should NOT happen if module serialization works
    results['status'] = 'error'
    results['error'] = f'NameError: {{e}}'
except Exception as e:
    results['status'] = 'error'
    results['error'] = str(e)

# Write results to the temp file
filepath = r'{result_filepath}'
try:
    with open(filepath, 'w') as f:
        json.dump(results, f)
except Exception as e:
    # Fallback if writing fails
    try:
        with open(filepath, 'w') as f_err:
            json.dump({{'status': 'write_error', 'error': str(e)}}, f_err)
    except Exception:
        pass # Ignore secondary write error

"""

    ip.run_cell(f"%%background\n{background_code}")

    # Wait for results
    max_wait_secs = 10
    start_wait = time.time()
    results_data = None
    while time.time() - start_wait < max_wait_secs:
        try:
            if os.path.exists(result_filepath) and os.path.getsize(result_filepath) > 0:
                with open(result_filepath, 'r') as f:
                    results_data = json.load(f)
                break
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.5)

    if os.path.exists(result_filepath):
        os.remove(result_filepath)

    # Assertions: Check if the background code could use the aliases
    assert results_data is not None, "Test results file was not created or was empty."
    assert results_data.get('status') == 'success', f"Background task failed: {results_data.get('error')}"
    assert results_data.get('df_shape') == [2, 1]
    assert results_data.get('arr_sum') == 30
    assert results_data.get('time_slept') is True

def capture_output_messages(ipython_shell, cell_code):
    """Runs a cell and captures display_pub messages (stdout, stderr, display_data)."""
    # Mock the display_pub.publish method to capture messages
    captured_messages = []
    original_publish = ipython_shell.display_pub.publish

    def capture_hook(data, metadata=None, parent=None, transient=None, update=False):
        # Capture relevant data based on message type inferred from data/transient
        msg = {
            'data': data,
            'metadata': metadata,
            'parent_msg_id': parent['header']['msg_id'] if parent and 'header' in parent and 'msg_id' in parent['header'] else None
        }
        if transient and 'stream' in transient:
            msg['msg_type'] = 'stream'
            msg['stream_name'] = transient['stream']['name']
            msg['text'] = transient['stream']['text']
        elif 'image/png' in data: # Simple check for plot-like data
            msg['msg_type'] = 'display_data'
            # Don't store large image data in test captures
            msg['data'] = {k: '...data...' if 'image' in k else v for k, v in data.items()}
        else:
            msg['msg_type'] = 'display_data' # Default assumption

        captured_messages.append(msg)
        # Call original publish if needed? Maybe not for testing isolation.
        # original_publish(data=data, metadata=metadata, parent=parent, transient=transient, update=update)

    ipython_shell.display_pub.publish = capture_hook

    # Get the message ID *before* running the cell
    parent = ipython_shell.kernel.get_parent()
    parent_msg_id = parent['header']['msg_id']

    # Run the cell that starts the background task
    ipython_shell.run_cell(cell_code)

    # Restore the original publish method
    ipython_shell.display_pub.publish = original_publish

    return parent_msg_id, captured_messages


# Requires a kernel environment to properly test parent_header based isolation
@pytest.mark.skipif(not pytest.importorskip("ipykernel"), reason="Requires ipykernel for proper header testing")
def test_output_isolation(ip):
    """Test that output from concurrent cells goes to the correct place."""

    if not hasattr(ip, 'kernel') or not ip.kernel or not hasattr(ip.kernel, 'session'):
         pytest.skip("IPython shell instance lacks a kernel or session for header testing.")

    cell1_code = """%%background
    import time
    print('CELL_1_OUTPUT_1')
    time.sleep(2)
    print('CELL_1_OUTPUT_2')
    """
    cell2_code = """%%background
    import time
    print('CELL_2_OUTPUT_1')
    time.sleep(1)
    print('CELL_2_OUTPUT_2')
    """

    # Mock the session.send method to capture messages
    original_session_send = ip.kernel.session.send
    all_captured_messages = [] # List to store tuples of (parent_msg_id, text_content)

    # Use MagicMock to allow arbitrary attribute access if needed by the code under test
    # Although session.send is directly patched here.
    mock_session = MagicMock(spec=ip.kernel.session)

    def capture_hook(stream, msg_type, content, parent=None, ident=None, buffers=None, track=False, header=None, metadata=None):
        parent_msg_id = parent['header']['msg_id'] if parent and 'header' in parent and 'msg_id' in parent['header'] else None
        text_content = None
        # We are interested in 'stream' messages for this test
        if msg_type == 'stream' and 'text' in content:
            text_content = content['text']
            all_captured_messages.append((parent_msg_id, text_content))

        # If we needed to simulate a reply, we could call original_session_send here,
        # but for capturing IOPub, we don't need to.
        # original_session_send(stream, msg_type, content, parent, ident, buffers, track, header, metadata)

    # Patch the send method on the actual session object
    ip.kernel.session.send = capture_hook

    try:
        # Run cell 1
        parent1 = ip.kernel.get_parent()
        parent1_msg_id = parent1['header']['msg_id']
        ip.run_cell(cell1_code)
        time.sleep(0.2) # Small delay

        # Run cell 2
        parent2 = ip.kernel.get_parent()
        parent2_msg_id = parent2['header']['msg_id']
        ip.run_cell(cell2_code)

        # Wait for background tasks
        time.sleep(3.0)

    finally:
        # IMPORTANT: Restore the original session.send method
        ip.kernel.session.send = original_session_send

    # Analyze captured messages
    outputs_for_cell1 = set()
    outputs_for_cell2 = set()

    for msg_parent_id, text_content in all_captured_messages:
        cleaned_text = text_content.strip()
        if msg_parent_id == parent1_msg_id:
            outputs_for_cell1.add(cleaned_text)
        elif msg_parent_id == parent2_msg_id:
            outputs_for_cell2.add(cleaned_text)

    # Assertions
    expected_cell1 = {'CELL_1_OUTPUT_1', 'CELL_1_OUTPUT_2'}
    expected_cell2 = {'CELL_2_OUTPUT_1', 'CELL_2_OUTPUT_2'}

    print("\nCaptured for Cell 1:", outputs_for_cell1)
    print("Captured for Cell 2:", outputs_for_cell2)

    assert outputs_for_cell1 == expected_cell1, "Output mismatch for cell 1"
    assert outputs_for_cell2 == expected_cell2, "Output mismatch for cell 2" 

def test_skips_unserializable_globals(ip):
    """Test that non-serializable globals are skipped with a warning, and others are passed."""
    # Define globals, including a non-serializable one (generator)
    ip.user_ns['good_var'] = "This should pass"
    ip.user_ns['bad_var'] = (x for x in range(3)) # Generator - typically not pickleable
    ip.user_ns['another_good'] = 12345

    # Temp file for results
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as tmpfile:
        result_filepath = tmpfile.name

    # Code to run in background
    background_code = f"""
import json

results = {{'status': 'success'}} # Default to success
try:
    # Check the good variables
    results['good_var_val'] = good_var
    results['another_good_val'] = another_good
except Exception as e:
    results['status'] = 'error'
    results['error'] = f'Error accessing good vars: {{e}}'

# Intentionally try accessing the bad variable, expecting NameError
try:
    print(bad_var)
    # If the above line *doesn't* raise NameError, something is wrong
    results['bad_var_accessible'] = True
except NameError:
    results['bad_var_accessible'] = False # Expected outcome
except Exception as e:
    # Other unexpected error
    results['status'] = 'error'
    results['error_accessing_bad'] = str(e)

# Write results
filepath = r'{result_filepath}'
try:
    with open(filepath, 'w') as f:
        json.dump(results, f)
except Exception as write_e:
    print(f"ERROR writing results: {{write_e}}") # Print error to potentially captured stderr

"""

    # Capture stderr to check for the warning message
    original_stderr_write = sys.stderr.write
    stderr_capture = StringIO()
    sys.stderr.write = stderr_capture.write

    try:
        ip.run_cell(f"%%background\n{background_code}")

        # Wait for results
        max_wait_secs = 5
        start_wait = time.time()
        results_data = None
        while time.time() - start_wait < max_wait_secs:
            try:
                if os.path.exists(result_filepath) and os.path.getsize(result_filepath) > 0:
                    with open(result_filepath, 'r') as f:
                        results_data = json.load(f)
                    break
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            time.sleep(0.2)
    finally:
        # Restore stderr and close capture
        sys.stderr.write = original_stderr_write
        captured_stderr = stderr_capture.getvalue()
        stderr_capture.close()
        if os.path.exists(result_filepath):
            os.remove(result_filepath)

    # Assertions
    print("Captured Stderr:\n", captured_stderr) # Print captured stderr for debugging if needed
    assert results_data is not None, "Test results file was not created or was empty."
    assert results_data.get('status') == 'success', f"Background task failed: {results_data.get('error')}"
    assert results_data.get('good_var_val') == "This should pass"
    assert results_data.get('another_good_val') == 12345
    assert results_data.get('bad_var_accessible') is False, "Non-serializable variable was unexpectedly accessible."
    # Check that the warning message was printed to stderr (might be fragile)
    assert "[Warning] Skipping non-serializable global variable 'bad_var'" in captured_stderr 