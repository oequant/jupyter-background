import pytest
from IPython.terminal.interactiveshell import TerminalInteractiveShell
import time
import pandas as pd
import numpy as np
import os
import sys
import io
from contextlib import redirect_stdout, redirect_stderr

@pytest.fixture(scope='function')
def ip():
    """Get a fresh IPython shell instance."""
    shell = TerminalInteractiveShell.instance()
    shell.reset(new_session=True)
    shell.run_line_magic('load_ext', 'background_magic')
    yield shell
    try:
        shell.run_line_magic('unload_ext', 'background_magic')
    except KeyError:
        pass
    shell.reset(new_session=True)

def test_global_variable_return(ip):
    """Test if variables defined in background are returned to global namespace."""
    # Run code in background that defines variables
    ip.run_cell("""%%background
import numpy as np
test_var = 42
test_arr = np.array([1, 2, 3])
test_dict = {'key': 'value'}
""")
    
    # Wait for background process to complete
    time.sleep(2)
    
    # Check if variables are available in global namespace
    assert ip.user_ns.get('test_var') == 42
    assert 'test_arr' in ip.user_ns
    np.testing.assert_array_equal(ip.user_ns['test_arr'], np.array([1, 2, 3]))
    assert ip.user_ns.get('test_dict') == {'key': 'value'}

def test_namespace_variable_isolation(ip):
    """Test if variables in namespaces are isolated correctly."""
    # Run code in first namespace
    ip.run_cell("""%%background ns1
x = 100
shared = "from_ns1"
""")
    
    # Run code in second namespace
    ip.run_cell("""%%background ns2
x = 200
shared = "from_ns2"
""")
    
    # Wait for background processes to complete
    time.sleep(2)
    
    # Variables should not be in global namespace
    assert 'x' not in ip.user_ns
    assert 'shared' not in ip.user_ns
    
    # Check namespace variables by running code in each namespace
    # that accesses the variables
    
    # Check ns1 variables
    result1 = ip.run_cell("""%%background ns1
print(f"x = {x}, shared = {shared}")
result = (x, shared)
""")
    
    time.sleep(1)
    assert ip.user_ns.get('result') == (100, "from_ns1")
    
    # Check ns2 variables
    result2 = ip.run_cell("""%%background ns2
print(f"x = {x}, shared = {shared}")
result = (x, shared)
""")
    
    time.sleep(1)
    assert ip.user_ns.get('result') == (200, "from_ns2")

def test_namespace_variable_persistence(ip):
    """Test if variables persist between cells in the same namespace."""
    # First cell in namespace
    ip.run_cell("""%%background persistent
step1 = True
data = [1, 2, 3]
""")
    
    time.sleep(1)
    
    # Second cell in same namespace should have access to previous variables
    ip.run_cell("""%%background persistent
assert step1 == True
data.append(4)
result = data
""")
    
    time.sleep(1)
    
    # Third cell verifies persistence
    ip.run_cell("""%%background persistent
assert data == [1, 2, 3, 4]
final_result = len(data)
""")
    
    time.sleep(1)
    
    # Check final result
    assert ip.user_ns.get('final_result') == 4

def test_unpicklable_object_handling(ip):
    """Test handling of unpicklable objects."""
    # Capture stderr to check for warning messages
    stderr_capture = io.StringIO()
    with redirect_stderr(stderr_capture):
        # Run code that creates both picklable and unpicklable objects
        ip.run_cell("""%%background
# Picklable variables
normal_var = 123
normal_list = [1, 2, 3]

# Import a module (potentially unpicklable)
import sys
module_var = sys

# Create an unpicklable object (e.g., a function with a reference to a system resource)
def unpicklable_function():
    return sys.stdout

func_var = unpicklable_function
""")
        
        # Wait for background process to complete
        time.sleep(2)
    
    # Check stderr for warning about skipped variables
    stderr_content = stderr_capture.getvalue()
    assert "Skipped non-serializable variables" in stderr_content
    
    # Picklable variables should be transferred
    assert ip.user_ns.get('normal_var') == 123
    assert ip.user_ns.get('normal_list') == [1, 2, 3]
    
    # Unpicklable variables should be skipped
    # Either they're not in user_ns or they're None
    if 'module_var' in ip.user_ns:
        assert ip.user_ns.get('module_var') is not sys
    if 'func_var' in ip.user_ns:
        assert not callable(ip.user_ns.get('func_var'))

def test_variable_modification_tracking(ip):
    """Test that only variables created or modified in the cell are returned."""
    # Set up initial variables
    ip.run_cell("initial_var = 'unchanged'")
    ip.run_cell("to_be_modified = 'original'")
    
    # Run background cell that creates new variables and modifies existing ones
    ip.run_cell("""%%background
# Create new variable
new_var = 'new value'

# Modify existing variable
to_be_modified = 'modified'

# Access but don't modify existing variable
print(f"Initial var: {initial_var}")
""")
    
    # Wait for background process to complete
    time.sleep(2)
    
    # Check variables
    assert 'new_var' in ip.user_ns
    assert ip.user_ns.get('new_var') == 'new value'
    assert ip.user_ns.get('to_be_modified') == 'modified'
    assert ip.user_ns.get('initial_var') == 'unchanged' 