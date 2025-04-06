import sys # Added for stderr debug printing
import time
import uuid
import threading
import hashlib # For hashing cell content
from multiprocessing import Process, Queue, Manager
import queue # Explicit import for queue.Empty
import cloudpickle # Import cloudpickle
from IPython.core.magic import Magics, line_magic, cell_magic, magics_class
from IPython.display import display, clear_output, HTML, publish_display_data as main_publish_display_data
from IPython import get_ipython

# Import the runner function
from .background_runner import run_code_in_background

# Function executed by the listener thread
def output_listener(output_queue: Queue, status_display_id: str, stop_event: threading.Event, parent_header):
    """Listens to the queue and displays output in the cell, associated with parent_header."""
    ipython = get_ipython()
    # Ensure kernel, session, and iopub_socket exist
    if not ipython or not hasattr(ipython, 'kernel') or not ipython.kernel \
       or not hasattr(ipython.kernel, 'iopub_socket') or not ipython.kernel.iopub_socket \
       or not hasattr(ipython.kernel, 'session') or not ipython.kernel.session:
        print("[Listener Error] Could not get IPython kernel, session, or iopub_socket.", file=sys.stderr)
        return # Cannot display output reliably

    session = ipython.kernel.session
    iopub_socket = ipython.kernel.iopub_socket # Get the actual socket

    running_indicator = ["/", "-", "\\\\", "|"]
    indicator_idx = 0
    start_time = time.time()
    update_interval = 0.5
    last_update_time = 0
    final_status = "Unknown"
    process_finished = False

    while not stop_event.is_set():
        try:
            timeout = 0.05 if process_finished else 0.1
            message = output_queue.get(timeout=timeout)
            msg_type, task_id, content = message

            # Ensure parent_header is valid before sending
            if not parent_header:
                # Fallback: Use display() which might not isolate output correctly
                # This might happen in test environments without a kernel.
                if msg_type == "stdout": print(content, end='')
                elif msg_type == "stderr": print(content, file=sys.stderr, end='')
                elif msg_type == "display_data":
                     try:
                         payload = cloudpickle.loads(content)
                         display(payload.get('data',{}), raw=True) # Display raw data dict
                     except Exception as e:
                          print(f"[Display Error - No Header] {e}", file=sys.stderr)
                continue # Skip status messages if no header

            # Send messages using session.send with the iopub_socket
            if msg_type == "stdout":
                stream_content = {'name': 'stdout', 'text': content}
                session.send(iopub_socket, 'stream', stream_content, parent=parent_header, ident=None)
            elif msg_type == "stderr":
                stream_content = {'name': 'stderr', 'text': content}
                session.send(iopub_socket, 'stream', stream_content, parent=parent_header, ident=None)
            elif msg_type == "display_data":
                try:
                    payload = cloudpickle.loads(content)
                    data = payload.get('data', {})
                    metadata = payload.get('metadata', {})
                    display_content = {
                        'data': data,
                        'metadata': metadata,
                        'transient': {}
                    }
                    session.send(iopub_socket, 'display_data', display_content, parent=parent_header, ident=None)
                except Exception as e:
                    err_text = f"[Display Error] {e}\n{traceback.format_exc()}"
                    stream_content = {'name': 'stderr', 'text': err_text}
                    # Send error back via stream message
                    session.send(iopub_socket, 'stream', stream_content, parent=parent_header, ident=None)

            elif msg_type == "status":
                # Status updates handled locally via display(id=...), no change
                if content == "running": last_update_time = time.time()
                elif content == "completed": final_status = "completed"
                elif content == "error": final_status = "error"
                elif content == "finished_processing":
                    process_finished = True
                    if final_status == "Unknown": final_status = "completed"

            last_update_time = time.time()

        except queue.Empty:
            # No message?
            if process_finished:
                # If the process finished sending AND the queue is now empty, stop listener.
                stop_event.set()
            elif not stop_event.is_set():
                # Update the running indicator only if the process hasn't finished
                current_time = time.time()
                if current_time - last_update_time >= update_interval:
                    elapsed_time = int(current_time - start_time)
                    indicator = running_indicator[indicator_idx % len(running_indicator)]
                    # Updated format: Running (Xs) Indicator
                    status_html = f"<div id='{status_display_id}' style='margin-bottom: 5px;'><i>Running ({elapsed_time}s) {indicator}</i></div>"
                    # Update status display - this still uses display() with id, which should be okay
                    # as it targets a specific element ID.
                    try:
                         display(HTML(status_html), display_id=status_display_id, update=True)
                    except Exception as display_err:
                         # Log if status update fails, but don't crash listener
                         print(f"[Listener Warning] Failed to update status display: {display_err}", file=sys.stderr)
                    last_update_time = current_time
                    indicator_idx += 1
            continue # Go back to checking the queue or timeout
        except Exception as e:
            # Log listener errors (shouldn't happen often)
            print(f"[Listener Error] {e}", file=sys.stderr)
            final_status = "listener_error"
            stop_event.set()
            break

    # --- Loop finished ---
    elapsed_time = int(time.time() - start_time)
    # Capitalize status for display
    display_status = final_status.capitalize()
    final_html = f"<div id='{status_display_id}' style='margin-bottom: 5px;'><i>Finished ({display_status}) - {elapsed_time}s</i></div>"
    # Final display update, ensure it happens even if loop exited quickly
    try:
        display(HTML(final_html), display_id=status_display_id, update=True)
    except Exception as display_err:
         print(f"[Listener Error] Could not display final status: {display_err}", file=sys.stderr)


@magics_class
class BackgroundMagics(Magics):
    def __init__(self, shell):
        super(BackgroundMagics, self).__init__(shell)
        # Store task info: {task_id: {'process': Process, 'listener': Thread, ...}}
        self._background_tasks = {}
        # Map cell content hash to running task_id
        self._cell_hash_to_task_id = {}
        self._task_counter = 0
        # Store the instance on the shell for unload_ipython_extension
        shell._background_magic_instance = self
        # Dictionary to store namespaces from background processes
        self._namespaces = {}
        # Manager for shared dictionaries
        self._manager = Manager()

    # Ensure cleanup happens when the Magics object is deleted (e.g., kernel restart)
    def __del__(self):
        # print("BackgroundMagics instance deleted. Attempting cleanup...") # Reduce verbosity
        self._unload_tasks()

    def _stop_task(self, task_id):
        """Stops and cleans up a specific background task by its ID."""
        task_info = self._background_tasks.pop(task_id, None)
        if not task_info: return

        # Find and remove the corresponding cell hash entry
        cell_hash_to_remove = None
        for cell_hash, running_task_id in self._cell_hash_to_task_id.items():
            if running_task_id == task_id:
                cell_hash_to_remove = cell_hash
                break
        if cell_hash_to_remove:
            del self._cell_hash_to_task_id[cell_hash_to_remove]

        # print(f"Stopping task {task_id}...") # Can be noisy
        process = task_info['process']
        listener = task_info['listener']
        stop_event = task_info['stop_event']
        status_display_id = task_info['status_display_id']

        try:
            if listener.is_alive(): stop_event.set()
            if process.is_alive():
                process.terminate()
                process.join(timeout=0.5)
                if process.is_alive(): process.kill(); process.join(timeout=0.2)
            if listener.is_alive(): listener.join(timeout=0.5)

            # Update status display to indicate it was stopped
            final_html = f"<div id='{status_display_id}' style='margin-bottom: 5px;'><i>Task stopped (superseded).</i></div>"
            try:
                display(HTML(final_html), display_id=status_display_id, update=True)
            except Exception:
                pass # Ignore display errors during potentially messy cleanup
        except Exception as e:
            print(f"    Error stopping task {task_id}: {e}", file=sys.stderr)

    def _unload_tasks(self):
        """Helper method to stop all tasks, used by __del__ and unload_ipython_extension."""
        if not hasattr(self, '_background_tasks') or not self._background_tasks:
             return
        print(f"Attempting to stop {len(self._background_tasks)} background task(s)...")
        tasks_to_remove = list(self._background_tasks.keys()) # Iterate over copy
        for task_id in tasks_to_remove:
             self._stop_task(task_id) # Use the refactored stop method
        print("Finished attempting to stop tasks.")
        self._cell_hash_to_task_id.clear() # Ensure mapping is cleared on unload

    @cell_magic
    def background(self, line, cell):
        """Execute cell in background, stopping previous run, directing output correctly."""
        # --- Check if a namespace was specified ---
        namespace = None
        if line.strip():
            # Parse the line for a namespace name
            namespace = line.strip()
        
        # --- Get parent header for associating output (if kernel exists) ---
        parent_header = None
        if hasattr(self.shell, 'kernel') and self.shell.kernel:
             parent_header = self.shell.kernel.get_parent()
        elif hasattr(self.shell, 'parent_header'): # Sometimes stored directly on shell?
             parent_header = self.shell.parent_header
        # If parent_header is still None, output might not be perfectly isolated in all clients.

        # --- Stop previous instance of this cell if running ---
        cell_content_hash = hashlib.sha1(cell.encode('utf-8')).hexdigest()
        previous_task_id = self._cell_hash_to_task_id.get(cell_content_hash)

        if previous_task_id and previous_task_id in self._background_tasks:
            # Check if the process associated with that task ID is actually still running
            previous_process = self._background_tasks[previous_task_id].get('process')
            if previous_process and previous_process.is_alive():
                print(f"Stopping previous background run for this cell (Task ID: {previous_task_id[:8]}...).", file=sys.stderr)
                self._stop_task(previous_task_id)
                time.sleep(0.1) # Brief pause to allow cleanup
            else:
                 # Process finished or task entry is stale, remove the mapping
                 self._background_tasks.pop(previous_task_id, None)
                 if cell_content_hash in self._cell_hash_to_task_id: # Avoid KeyError if already removed by _stop_task
                     del self._cell_hash_to_task_id[cell_content_hash]

        # --- Proceed with starting the new task ---
        self._task_counter += 1
        base_id = f"bg_task_{self._task_counter}"
        task_id = f"{base_id}_{uuid.uuid4().hex[:8]}"
        status_display_id = f"status_{task_id}"
        output_queue = Queue()
        stop_event = threading.Event()

        # --- Capture and serialize global context ---
        user_ns = self.shell.user_ns
        serializable_ns = {}
        skipped_keys = []
        # Common IPython variables to exclude from serialization
        ipython_builtins_to_skip = {
            'In', 'Out', 'get_ipython', 'exit', 'quit',
            '_', '__', '___', '_i', '_ii', '_iii', '_ih', '_oh', '_dh'
            # Add others if necessary
        }

        # If we have a namespace, check if it exists and use its variables
        if namespace and namespace in self._namespaces:
            namespace_dict = self._namespaces[namespace]
            for k, v in namespace_dict.items():
                if k not in ipython_builtins_to_skip:
                    serializable_ns[k] = v
            print(f"[Info] Using variables from namespace '{namespace}'", file=sys.stderr)

        # Add global variables
        for k, v in user_ns.items():
            # Check if key should be skipped *first*
            if (k.startswith('_') and k not in ('_', '__', '___')) or k in ipython_builtins_to_skip:
                 skipped_keys.append(k)
                 continue

            # If not skipped, *then* try to pickle
            try:
                cloudpickle.dumps(v)
                serializable_ns[k] = v
            except Exception as pickle_err:
                # If pickling fails for a variable we didn't intend to skip, warn and skip it.
                skipped_keys.append(k)
                print(f"[Warning] Skipping non-serializable global variable '{k}' (type: {type(v).__name__}). Error: {pickle_err}", file=sys.stderr)
                # No need to continue here, the loop naturally proceeds

        # Now, serialize the dictionary of successfully pickled items
        try:
            serialized_context = cloudpickle.dumps(serializable_ns)
        except Exception as e:
            print(f"[Error] Failed to serialize the collected global context: {e}", file=sys.stderr)
            serialized_context = None # Signal to runner that context failed

        # Create a manager dict to receive variables from the background process
        result_dict = self._manager.dict()

        initial_status_html = f"<div id='{status_display_id}' style='margin-bottom: 5px;'><i>Starting [{base_id}]...</i></div>"
        display(HTML(initial_status_html), display_id=status_display_id)

        # --- Start listener thread, passing parent_header ---
        listener = threading.Thread(
            target=output_listener,
            args=(output_queue, status_display_id, stop_event, parent_header),
            daemon=True
        )
        listener.start()

        process = Process(
            target=run_code_in_background,
            # Pass serialized context to the runner function
            args=(cell, output_queue, task_id, serialized_context, result_dict),
            daemon=True
        )
        process.start()

        # Store task info and update cell hash mapping
        self._background_tasks[task_id] = {
            'process': process,
            'listener': listener,
            'queue': output_queue,
            'stop_event': stop_event,
            'status_display_id': status_display_id,
            'cell_hash': cell_content_hash, # Store hash for potential reverse lookup
            'namespace': namespace,
            'result_dict': result_dict,
            'transfer_complete': threading.Event()  # Add event for tracking transfer completion
        }
        self._cell_hash_to_task_id[cell_content_hash] = task_id # Map hash to new task ID
        
        # Start a thread to handle the transfer of variables after the process completes
        var_transfer_thread = threading.Thread(
            target=self._handle_variable_transfer,
            args=(task_id,),
            daemon=True
        )
        var_transfer_thread.start()
        
        # Block briefly to ensure short-running cells have time to transfer variables
        # This helps with cells that finish very quickly
        time.sleep(0.2)
        
        # Check if the 'res' variable is critical for this cell
        needs_res = 'res' in cell
        if needs_res:
            # Wait up to 5 seconds for variable transfer to complete if it has 'res'
            task_info = self._background_tasks.get(task_id)
            if task_info and 'transfer_complete' in task_info:
                task_info['transfer_complete'].wait(5.0)
                
            # Log warning if still waiting
            if task_info and 'transfer_complete' in task_info and not task_info['transfer_complete'].is_set():
                print(f"[Warning] Variable transfer may still be in progress. 'res' variable may not be available immediately.", file=sys.stderr)

    def _handle_variable_transfer(self, task_id):
        """Wait for process to complete and transfer variables."""
        if task_id not in self._background_tasks:
            return
            
        task_info = self._background_tasks[task_id]
        process = task_info['process']
        namespace = task_info['namespace']
        result_dict = task_info['result_dict']
        transfer_complete_event = task_info.get('transfer_complete')
        
        # Wait for process to complete with timeout
        max_wait = 120  # Maximum wait time in seconds - increased for long-running tasks
        start_time = time.time()
        
        # Check periodically for variables while the process is running
        while process.is_alive() and time.time() - start_time < max_wait:
            # Check if variables are already available even while process is still running
            if '__transfer_complete__' in result_dict:
                break
            time.sleep(0.5)  # Increased sleep time to reduce CPU usage
        
        # Wait a short time after the process completes to ensure variable transfer is done
        if not process.is_alive():
            time.sleep(0.5)
        
        # Get variables from result_dict (excluding special keys)
        transferred_vars = {k: v for k, v in result_dict.items() 
                           if not k.startswith('__') and k != '__transfer_complete__'}
        
        if not transferred_vars:
            print(f"[Warning] No variables returned from background task {task_id[:8]}", file=sys.stderr)
            if transfer_complete_event:
                transfer_complete_event.set()  # Mark as complete even if no variables
            return
            
        # If namespace is specified, store variables in that namespace
        if namespace:
            if namespace not in self._namespaces:
                self._namespaces[namespace] = {}
            self._namespaces[namespace].update(transferred_vars)
            print(f"[Info] Variables from background task stored in namespace '{namespace}'", file=sys.stderr)
            var_names = list(transferred_vars.keys())
            if var_names:
                print(f"[Debug] Transferred variables to namespace '{namespace}': {', '.join(var_names)}", file=sys.stderr)
        else:
            # Update global namespace
            self.shell.user_ns.update(transferred_vars)
            var_names = list(transferred_vars.keys())
            if var_names:
                print(f"[Info] Variables from background task updated in global namespace", file=sys.stderr)
                print(f"[Debug] Transferred variables: {', '.join(var_names)}", file=sys.stderr)
            
            # Signal that transfer is complete
            if transfer_complete_event:
                transfer_complete_event.set()


def load_ipython_extension(ipython):
    """Register the BackgroundMagics class with IPython."""
    # Need to handle potential exceptions during QueueStream import if multiprocessing isn't available?
    # Assuming standard library availability here.
    ipython.register_magics(BackgroundMagics)

# Optional: Function to unload the magic
def unload_ipython_extension(ipython):
    """Unregister the BackgroundMagics class and attempt task cleanup."""
    print("Unloading Background Magic extension...")
    magics_instance = getattr(ipython, '_background_magic_instance', None)
    if magics_instance:
        magics_instance._unload_tasks() # Use helper method
        # Remove instance reference from shell
        try:
            delattr(ipython, '_background_magic_instance')
        except AttributeError:
            pass # Ignore if already gone
    else:
        print("No active magic instance found.")
    print("Background magic extension unloaded.")

# Store instance for potential cleanup during unload
# Moved instance storage into __init__

# Ensure queue module is imported for the Empty exception catch in listener
# (already imported at the top now) 