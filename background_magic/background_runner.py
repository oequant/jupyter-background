"""Module to handle running code in a background process."""

import sys
import traceback
from multiprocessing import Process, Queue
from io import StringIO
import contextlib
import cloudpickle
# Only import basic IPython display functions needed globally
from IPython import get_ipython
from IPython.display import display as ipy_display, publish_display_data as ipython_publish_display_data
# Defer matplotlib import
# import matplotlib
# from IPython.core.interactiveshell import InteractiveShell
# from IPython.core import pylabtools as core_pylabtools

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

    # --- Configure Matplotlib Backend ---
    try:
        import matplotlib
        # Set a non-interactive backend for figure generation
        matplotlib.use('Agg')
        # We rely on the patched publish_display_data being picked up by plt.show()
        # print("[Debug] Matplotlib backend set to Agg.", file=stderr_stream)
    except ImportError:
        # Matplotlib not available, ignore
        pass
    except Exception as backend_err:
        print(f"[Warning] Failed to set Matplotlib backend: {backend_err}", file=stderr_stream)
    # --- End Matplotlib Config ---

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