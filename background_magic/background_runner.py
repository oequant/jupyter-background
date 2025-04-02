"""Module to handle running code in a background process."""

import sys
import os
import io
import uuid
import traceback
from multiprocessing import Process, Queue
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

def run_code_in_background(code_str: str, output_queue: Queue, task_id: str, serialized_context: bytes | None):
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
        with contextlib.redirect_stdout(stdout_stream), contextlib.redirect_stderr(stderr_stream):
            exec(code_str, exec_globals)
        output_queue.put(("status", task_id, "completed"))

    except Exception as e:
        tb_str = traceback.format_exc()
        output_queue.put(("stderr", task_id, tb_str))
        output_queue.put(("status", task_id, "error"))
    finally:
        # No specific display hook cleanup needed with this approach
        output_queue.put(("status", task_id, "finished_processing")) 