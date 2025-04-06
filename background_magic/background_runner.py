"""Module to handle running code in a background process."""

import sys
import os
import io
import uuid
import traceback
from multiprocessing import Process, Queue, Manager
from io import StringIO
import contextlib
import cloudpickle
from base64 import b64encode
# Only import basic IPython display functions needed globally
from IPython import get_ipython
from IPython.display import display as ipy_display, publish_display_data as ipython_publish_display_data, HTML, Markdown, IFrame

# Custom stream wrapper to write to the queue
class QueueStream(StringIO):
    def __init__(self, queue: Queue, task_id: str, stream_type: str):
        self.queue = queue
        self.task_id = task_id
        self.stream_type = stream_type # 'stdout' or 'stderr'

    def write(self, buf):
        self.queue.put((self.stream_type, self.task_id, buf))

    def flush(self):
        # Optionally implement flush if needed, though often not required for queues
        pass

# Custom display publisher that sends data over the queue
class QueueDisplayPublisher:
    def __init__(self, queue: Queue, task_id: str):
        self.queue = queue
        self.task_id = task_id

    def publish(self, data, metadata=None, **kwargs):
        if metadata is None:
            metadata = {}
        # Serialize and send display data
        try:
            payload = cloudpickle.dumps({'data': data, 'metadata': metadata})
            self.queue.put(('display_data', self.task_id, payload))
        except Exception as e:
            # Send serialization error back as stderr
            tb_str = f"Error serializing display data: {e}\n{traceback.format_exc()}"
            self.queue.put(("stderr", self.task_id, tb_str))

    # Implement other methods if needed by display logic (often not necessary)
    # def display(self, data, metadata=None, **kwargs): # publish handles display
    #     self.publish(data, metadata=metadata)
    # def update_display(self, data, metadata=None, display_id=None, **kwargs):
        # Basic implementation: treat update as new display for simplicity
    #     self.publish(data, metadata=metadata)

def is_module_or_unpicklable(obj):
    """Check if an object is a module or otherwise likely to be unpicklable."""
    # Check if it's a module
    if isinstance(obj, type(sys)):
        return True
    
    # Check if it has module-like attributes
    if hasattr(obj, '__name__') and hasattr(obj, '__spec__') and hasattr(obj, '__package__'):
        return True
    
    # Check if it's a function that refers to modules
    if callable(obj) and hasattr(obj, '__module__') and obj.__module__ in sys.modules:
        # Functions defined in modules should be fine with cloudpickle, but
        # check if it contains references to sys or other known problematic modules
        return False
        
    return False

def run_code_in_background(code_str: str, output_queue: Queue, task_id: str, serialized_context: bytes | None, result_dict=None):
    """Executes code, capturing stdout/stderr and display outputs."""
    # --- Initialize execution context ---
    exec_globals = globals().copy()
    if serialized_context:
        try:
            deserialized_ns = cloudpickle.loads(serialized_context)
            exec_globals.update(deserialized_ns)
        except Exception as e:
            tb_str = f"Error deserializing context: {e}\n{traceback.format_exc()}"
            output_queue.put(("stderr", task_id, tb_str))
            output_queue.put(("status", task_id, "error"))
            output_queue.put(("status", task_id, "finished_processing"))
            return
    elif serialized_context is None:
         output_queue.put(("stderr", task_id, "[Warning] Global context serialization failed, running without it.\n"))

    # --- Identify explicitly assigned variables ---
    # Simple parsing to find variable assignments in the code
    explicit_vars = set()
    loop_vars = set()
    
    # Define important variables that should be included if they exist
    important_vars = ['res', 'result', 'i', 'df', 'data']
    
    for line in code_str.split('\n'):
        line = line.strip()
        # Skip comments and empty lines
        if not line or line.startswith('#'):
            continue
            
        # Look for assignments (=) not in conditionals/loops
        if '=' in line and not line.startswith(('if ', 'for ', 'while ', 'def ', 'class ')):
            # Handle multi-assignments: x, y = 1, 2
            var_part = line.split('=')[0].strip()
            
            # Handle list/tuple assignments with comma separators
            for name in var_part.split(','):
                name = name.strip()
                if name and name.isidentifier():
                    explicit_vars.add(name)
                    
        # Also look for append operations on lists: res.append(i)
        elif '.append(' in line:
            parts = line.split('.append(')
            if parts and parts[0].strip().isidentifier():
                explicit_vars.add(parts[0].strip())
                
        # Also capture for loop variables: for i in range(5)
        elif line.startswith('for ') and ' in ' in line:
            var_part = line[4:].split(' in ')[0].strip()
            for name in var_part.split(','):
                name = name.strip()
                if name and name.isidentifier():
                    loop_vars.add(name)
    
    # Add loop variables to explicit vars
    explicit_vars.update(loop_vars)
    
    if explicit_vars:
        output_queue.put(("stderr", task_id, f"[Debug] Detected variable assignments: {', '.join(explicit_vars)}\n"))

    # --- Setup output redirection AND display hook ---
    stdout_stream = QueueStream(output_queue, task_id, 'stdout')
    stderr_stream = QueueStream(output_queue, task_id, 'stderr')
    display_pub = QueueDisplayPublisher(output_queue, task_id)

    def custom_publish_display_data(data, metadata=None, **kwargs):
        """Sends display data over the queue instead of to ZMQ."""
        display_pub.publish(data, metadata)
    # Inject the *custom* publisher into the globals for the exec call
    exec_globals['publish_display_data'] = custom_publish_display_data
    
    # Custom display function for use in the background process
    def custom_display(obj):
        """Custom display function that routes through our queue system."""
        if hasattr(obj, '_repr_html_'):
            try:
                html = obj._repr_html_()
                if html:
                    display_pub.publish({'text/html': html})
                    return
            except Exception as e:
                print(f"[Warning] HTML display error: {e}", file=stderr_stream)
                
        # Fall back to repr if no HTML representation
        try:
            display_pub.publish({'text/plain': repr(obj)})
        except Exception as e:
            print(f"[Warning] Plain text display error: {e}", file=stderr_stream)
    
    # Add our custom display function to the globals
    from IPython import display as ipd
    exec_globals['display'] = custom_display
    exec_globals['HTML'] = HTML
    exec_globals['Markdown'] = Markdown
    exec_globals['IFrame'] = IFrame
    exec_globals['ipd'] = ipd

    # --- Configure Matplotlib for background process ---
    try:
        import matplotlib
        import matplotlib.pyplot as plt
        import tempfile
        
        # Set a non-interactive backend for figure generation
        matplotlib.use('Agg')
        
        # Override plt.show() to save and display figures in background process
        def patched_show(*args, **kwargs):
            for num, figmanager in enumerate(matplotlib._pylab_helpers.Gcf.get_all_fig_managers()):
                fig = figmanager.canvas.figure
                
                # Create in-memory file-like object for PNG data
                buf = io.BytesIO()
                fig.savefig(buf, format='png', bbox_inches='tight')
                buf.seek(0)
                
                # Encode image as base64 for HTML display
                data = b64encode(buf.getvalue()).decode('utf-8')
                html = f'<img src="data:image/png;base64,{data}">'
                
                # Send the image to the frontend via our custom display publisher
                display_pub.publish({'text/html': html})
                print("[Debug] Matplotlib figure displayed", file=stdout_stream)
                
                # Also support other formats like SVG if needed
                # Implement additional formats here if needed
            
            # Clear the current figure to avoid double displaying
            plt.clf()
            plt.close('all')
            
            return None
        
        # Override the original show function
        plt.show = patched_show
        exec_globals['plt'] = plt
        print("[Debug] Matplotlib patched successfully", file=stdout_stream)
        
    except ImportError:
        # Matplotlib not available, ignore
        pass
    except Exception as backend_err:
        print(f"[Warning] Failed to set Matplotlib backend: {backend_err}", file=stderr_stream)
    # --- End Matplotlib Config ---
    
    # --- Configure Plotly for background process ---
    try:
        # Try to import and patch plotly
        import plotly
        import plotly.graph_objects as go
        from plotly.io import to_html
        import plotly.io as pio
        
        # Print version info
        print(f"[Debug] Using Plotly version: {plotly.__version__}", file=stdout_stream)
        
        # Create plotly display function that uses static PNG approach with HTML file link
        def display_plotly_figure(fig):
            try:
                print(f"[Debug] Display called for figure type: {type(fig)}", file=stdout_stream)
                
                # Step 1: Save the figure to an HTML file
                html_filename = f"plotly_figure_{uuid.uuid4().hex[:8]}.html"
                fig.write_html(html_filename, include_plotlyjs='cdn', full_html=True)
                print(f"[Debug] Plotly figure saved to {html_filename}", file=stdout_stream)
                
                # Step 2: Generate a static PNG image
                try:
                    img_bytes = fig.to_image(format="png", scale=2, engine="kaleido")
                    img_b64 = b64encode(img_bytes).decode('utf-8')
                    
                    # Step 3: Create HTML with static image and download link
                    download_html = f'''
                    <div style="margin: 10px 0;">
                        <a href="{html_filename}" download="{html_filename}" target="_blank" 
                           style="background-color: #4CAF50; color: white; padding: 5px 10px; 
                                  text-decoration: none; font-weight: bold; border-radius: 4px;">
                            ⬇️ Download Interactive Plot
                        </a>
                        <span style="margin-left: 10px; color: #666;">
                            (static preview below, download for interactive version)
                        </span>
                    </div>
                    <div>
                        <img src="data:image/png;base64,{img_b64}" style="max-width:100%; border: 1px solid #ddd;">
                    </div>
                    '''
                    
                    # Display the combined HTML
                    custom_display(HTML(download_html))
                    print("[Debug] Static image with download link displayed", file=stdout_stream)
                    return
                    
                except Exception as img_err:
                    print(f"[Warning] Static image generation failed: {img_err}", file=stderr_stream)
                    
                    # Fallback to just the download link if image fails
                    fallback_html = f'''
                    <div style="padding: 20px; background-color: #f8f9fa; border: 1px solid #ddd; margin: 10px 0;">
                        <p>Interactive plot saved to <code>{html_filename}</code></p>
                        <a href="{html_filename}" download="{html_filename}" target="_blank" 
                           style="background-color: #4CAF50; color: white; padding: 8px 15px; 
                                  text-decoration: none; font-weight: bold; display: inline-block;
                                  border-radius: 4px; margin-top: 10px;">
                            ⬇️ Download Interactive Plot
                        </a>
                    </div>
                    '''
                    custom_display(HTML(fallback_html))
                    print("[Debug] Download link displayed (no preview available)", file=stdout_stream)
                    return
                
            except Exception as e:
                print(f"[Error] Plotly display error: {e}\n{traceback.format_exc()}", file=stderr_stream)
        
        # Simple patch for fig.show() method
        def patched_figure_show(self, *args, **kwargs):
            print("[Debug] fig.show() called", file=stdout_stream)
            display_plotly_figure(self)
            return None
        
        # Patch plotly Figure class
        go.Figure.show = patched_figure_show
        
        # Create a special save_and_show_figure function that users can call directly
        def save_and_show_figure(fig, filename=None):
            """Save the figure to a file and display it as a static image with download link."""
            print("[Debug] save_and_show_figure called", file=stdout_stream)
            display_plotly_figure(fig)
            return
        
        # Add the function to the globals
        exec_globals['save_and_show_figure'] = save_and_show_figure
        
        # Patch pio.show
        def patched_pio_show(fig, *args, **kwargs):
            print("[Debug] pio.show() called", file=stdout_stream)
            display_plotly_figure(fig)
            return None
        
        pio.show = patched_pio_show
        
        # Add imports to globals
        try:
            import plotly.express as px
            exec_globals['px'] = px
            print("[Debug] Plotly Express available", file=stdout_stream)
        except ImportError:
            print("[Warning] Plotly Express not available", file=stderr_stream)
        
        exec_globals['plotly'] = plotly
        exec_globals['go'] = go
        exec_globals['pio'] = pio
        
        print("[Debug] Plotly successfully patched", file=stdout_stream)
        
    except ImportError as ie:
        print(f"[Warning] Plotly import error: {ie}", file=stderr_stream)
    except Exception as plotly_err:
        print(f"[Warning] Failed to configure Plotly: {plotly_err}\n{traceback.format_exc()}", file=stderr_stream)
    # --- End Plotly Config ---

    try:
        output_queue.put(("status", task_id, "running"))
        
        # Store initial variable keys to track new or modified variables
        initial_var_keys = set(exec_globals.keys())
        
        with contextlib.redirect_stdout(stdout_stream), contextlib.redirect_stderr(stderr_stream):
            exec(code_str, exec_globals)
        output_queue.put(("status", task_id, "completed"))
        
        # Check for important variables and add them to explicit vars
        for var in important_vars:
            if var in exec_globals and var not in initial_var_keys:
                explicit_vars.add(var)
                output_queue.put(("stderr", task_id, f"[Debug] Adding important variable: {var}\n"))
        
        # Collect globals and send back via manager dict if provided
        if result_dict is not None:
            # Track only new or potentially modified variables
            current_var_keys = set(exec_globals.keys())
            new_or_modified_keys = current_var_keys - initial_var_keys
            
            # Add variables that existed before but might have been modified
            potentially_modified = initial_var_keys - set(['__builtins__', 'contextlib', 'QueueStream', 
                                         'QueueDisplayPublisher', 'traceback', 'cloudpickle'])
            
            # Variables to transfer (new + potentially modified)
            vars_to_transfer = new_or_modified_keys.union(potentially_modified)
            
            # Debug log for variable tracking
            output_queue.put(("stderr", task_id, f"[Debug] New variables: {', '.join(sorted(new_or_modified_keys)[:20])}\n"))
            
            # Filter out non-serializable objects before sending back
            serializable_globals = {}
            skipped_vars = []
            
            # Process explicit assignments first to make sure they are included
            for key in explicit_vars:
                if key in exec_globals and key not in ('__builtins__', 'contextlib', 'QueueStream', 
                                               'QueueDisplayPublisher', 'traceback', 'cloudpickle'):
                    value = exec_globals[key]
                    
                    # Special handling for simple data types that should always work
                    if isinstance(value, (int, float, str, bool, list, dict, tuple, set)):
                        serializable_globals[key] = value
                        output_queue.put(("stderr", task_id, f"[Debug] Including simple variable: {key} (type: {type(value).__name__})\n"))
                        continue
                    
                    try:
                        # Test if object can be pickled with cloudpickle
                        cloudpickle.dumps(value)
                        serializable_globals[key] = value
                        output_queue.put(("stderr", task_id, f"[Debug] Including explicitly assigned variable: {key}\n"))
                    except Exception as e:
                        skipped_vars.append(key)
                        output_queue.put(("stderr", task_id, f"[Warning] Cannot serialize explicitly assigned variable '{key}': {str(e)[:100]}...\n"))
            
            # Process other variables
            for key in vars_to_transfer:
                # Skip explicit vars (already processed) and internal objects
                if key in explicit_vars or key.startswith('__') or key in ('__builtins__', 'contextlib', 'QueueStream', 
                                                 'QueueDisplayPublisher', 'traceback', 'cloudpickle'):
                    continue
                
                value = exec_globals[key]
                
                # Skip module objects and other known unpicklable objects
                if is_module_or_unpicklable(value):
                    skipped_vars.append(key)
                    continue
                
                # Always include simple data types and collections
                if isinstance(value, (int, float, str, bool, list, dict, tuple, set)):
                    serializable_globals[key] = value
                    continue
                
                try:
                    # Test if object can be pickled with cloudpickle
                    cloudpickle.dumps(value)
                    serializable_globals[key] = value
                except Exception as e:
                    # Skip non-serializable objects but record them
                    skipped_vars.append(key)
                    output_queue.put(("stderr", task_id, f"[Warning] Cannot serialize variable '{key}': {str(e)[:100]}...\n"))
                    continue
            
            # Send serialized globals back via manager dict
            try:
                # Use simpler approach: directly add variables to result_dict
                # without additional serialization/deserialization
                success_vars = []
                
                for key, value in serializable_globals.items():
                    try:
                        # Add directly to result dict
                        result_dict[key] = value
                        success_vars.append(key)
                    except Exception as e:
                        skipped_vars.append(key)
                        output_queue.put(("stderr", task_id, f"[Warning] Failed to transfer variable '{key}': {str(e)[:100]}...\n"))
                
                # Mark as completed so main process knows variables are ready
                if success_vars:
                    result_dict['__transfer_complete__'] = True
                    var_count = len(success_vars)
                    output_queue.put(("stdout", task_id, f"[Info] {var_count} variables returned to main process.\n"))
                    
                    if var_count > 0:
                        output_queue.put(("stdout", task_id, f"[Info] Returned variables: {', '.join(success_vars[:20])}" + 
                                       (f" and {len(success_vars) - 20} more" if len(success_vars) > 20 else "") + "\n"))
                else:
                    output_queue.put(("stderr", task_id, "[Warning] No variables could be transferred to main process.\n"))
                
                if skipped_vars:
                    output_queue.put(("stderr", task_id, f"[Warning] Skipped non-transferable variables: {', '.join(skipped_vars)}\n"))
            except Exception as e:
                output_queue.put(("stderr", task_id, f"[Warning] Failed to process variables: {e}\n"))

    except Exception as e:
        tb_str = traceback.format_exc()
        output_queue.put(("stderr", task_id, tb_str))
        output_queue.put(("status", task_id, "error"))
    finally:
        # No specific display hook cleanup needed with this approach
        output_queue.put(("status", task_id, "finished_processing")) 