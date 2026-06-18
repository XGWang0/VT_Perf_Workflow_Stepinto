import asyncio
import copy
import json
import os
import pprint
import threading
import tomllib
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from jinja2 import Template
import paramiko

app = FastAPI()

class ProductMergeRequest(BaseModel):
    host: str
    product: str
    milestone: str
    build_ver: str
    session_id: str

# Helper function to recursively deep merge dictionary overrides
def deep_merge(dict1, dict2):
    """
    Recursively merges dict2 into dict1, overwriting values at matching paths.
    """
    for key, value in dict2.items():
        if isinstance(value, dict) and key in dict1 and isinstance(dict1[key], dict):
            deep_merge(dict1[key], value)
        else:
            dict1[key] = value
    return dict1

def flatten_dict(d, parent_key='', sep='_'):
    """
    Flattens nested dictionaries recursively to harvest scalar parameters for formatting.
    """
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        elif isinstance(v, (str, int, float, bool)):
            items.append((k, str(v)))
            items.append((new_key, str(v)))
    return dict(items)

def resolve_placeholders(item, context):
    """
    Recursively replaces {placeholder} tags inside string values with context values.
    Preserves unmatched braces using a SafeFormatter dictionary fallback.
    """
    if isinstance(item, dict):
        return {k: resolve_placeholders(v, context) for k, v in item.items()}
    elif isinstance(item, list):
        return [resolve_placeholders(i, context) for i in item]
    elif isinstance(item, str):
        if "{" in item and "}" in item:
            try:
                class SafeFormatter(dict):
                    def __missing__(self, key):
                        return f"{{{key}}}"
                return item.format_map(SafeFormatter(context))
            except Exception as err:
                print(f"Placeholder replacement exception for '{item}': {err}")
        return item
    else:
        return item

# Concurrency Protection: Thread-safe session configs registry to isolate multiple users.
# Keys are front-end generated unique Session IDs; values are their deep-merged TOML dicts.
session_configs = {}

# Symmetrical fallback context used on boot/startup or if a session hasn't merged yet
default_config_context = {}

def load_default_config_context():
    global default_config_context
    master_config_path = "config/vt_perf_auto.toml"
    machine_config_path = "config/machine_config/ph047.toml"
    
    final_config_dict = {}
    
    # 1. Parse Master TOML
    if os.path.exists(master_config_path):
        try:
            with open(master_config_path, "rb") as f:
                final_config_dict = tomllib.load(f)
        except Exception as err:
            print(f"Error parsing master TOML during boot: {err}")
            
    # 2. Parse and Overwrite/Merge Machine TOML (on top of master config!)
    if os.path.exists(machine_config_path):
        try:
            with open(machine_config_path, "rb") as f:
                machine_dict = tomllib.load(f)
            final_config_dict = deep_merge(copy.deepcopy(final_config_dict), machine_dict)
        except Exception as err:
            print(f"Error parsing machine TOML ph047 during boot: {err}")
            
    # 3. Create context mapping for {placeholder} replacement
    context = flatten_dict(final_config_dict)
    context.update({
        "host": "ph047",
        "hostname": "ph047",
        "product": "sles-15-sp7",
        "milestone": "GM",
        "build_ver": "Build44.4",
        "build_num": "44.4",
        "remote_ip": "ph047"
    })
    
    # 4. Recursively resolve placeholders
    final_config_dict = resolve_placeholders(final_config_dict, context)
    
    # Store into fallback context!
    default_config_context = final_config_dict

# Initialize default context on server startup
load_default_config_context()

def parse_lightweight_yaml(yaml_text):
    """
    Lightweight YAML parser that handles workflow_name, workflow_steps, and multiline script_content.
    No external dependencies required!
    """
    lines = yaml_text.splitlines()
    result = {"workflow_name": "", "workflow_steps": []}
    
    i = 0
    current_step = None
    in_script_content = False
    script_indent = 0
    script_lines = []
    
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        
        # If we are parsing a multiline script content block (indented block scalar)
        if in_script_content:
            # If the line is empty (no chars or spaces only), preserve it as an empty line in bash script!
            if not stripped:
                script_lines.append("")
                i += 1
                continue
            
            # Check indentation of the line
            if line.startswith(" " * script_indent):
                # Append the line (stripping only the base block indentation!)
                script_lines.append(line[script_indent:])
                i += 1
                continue
            else:
                # End of script_content block because indentation dropped!
                if current_step:
                    current_step["script_content"] = "\n".join(script_lines).rstrip()
                in_script_content = False
                script_lines = []
                # Do NOT increment i here, so this line is processed normally as a standard YAML entry!
        
        # If we are NOT inside a script content block, we can safely skip empty lines and YAML comments starting with '#'
        if not in_script_content:
            if not stripped or stripped.startswith("#"):
                i += 1
                continue
        
        # Parse workflow_name
        if line.startswith("workflow_name:"):
            val = line.split(":", 1)[1].strip().strip('"').strip("'")
            result["workflow_name"] = val
            i += 1
            continue
            
        # Parse workflow_steps list start
        if line.startswith("workflow_steps:"):
            i += 1
            continue
            
        # Parse list item
        if stripped.startswith("- steps_name:"):
            # Save previous step if any
            if current_step:
                result["workflow_steps"].append(current_step)
            
            val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            current_step = {
                "steps_name": val,
                "script_name": "",
                "script_content": "",
                "script_instroduction": ""
            }
            i += 1
            continue
            
        if current_step:
            if stripped.startswith("script_name:"):
                val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                current_step["script_name"] = val
            elif stripped.startswith("script_instroduction:"):
                val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                current_step["script_instroduction"] = val
            elif stripped.startswith("script_content: |"):
                in_script_content = True
                # Calculate indentation of the block
                script_indent = len(line) - len(line.lstrip()) + 4 # standard YAML block indent is 4 or 6 spaces
                # Let's dynamically find the indent of the next non-empty line to be extremely safe!
                next_idx = i + 1
                while next_idx < len(lines) and not lines[next_idx].strip():
                    next_idx += 1
                if next_idx < len(lines):
                    script_indent = len(lines[next_idx]) - len(lines[next_idx].lstrip())
                script_lines = []
        
        i += 1
    
    # Save last step
    if current_step:
        if in_script_content and script_lines:
            current_step["script_content"] = "\n".join(script_lines).rstrip()
        result["workflow_steps"].append(current_step)
        
    return result

@app.get("/config/workflows")
async def get_dynamic_workflows():
    workflow_dir = "workflow"
    results = []
    
    if os.path.exists(workflow_dir):
        # Scan and sort files alphabetically (e.g. 01.SUT... -> 02 -> 03 -> 04)
        files = sorted([f for f in os.listdir(workflow_dir) if f.endswith(".yaml") or f.endswith(".yml")])
        for filename in files:
            file_path = os.path.join(workflow_dir, filename)
            try:
                # Read only workflow_name to keep initial load lightning-fast (No TOML parsing!)
                workflow_name = "Stage"
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("workflow_name:"):
                            workflow_name = line.split(":", 1)[1].strip().strip('"').strip("'")
                            break
                # Parse stage index prefix (e.g. "01" from "01.SUT_Environment_Prepare.yaml")
                stage_index = filename.split(".", 1)[0] if "." in filename else "01"
                
                results.append({
                    "file_name": filename,
                    "stage_index": stage_index,
                    "workflow_name": workflow_name
                })
            except Exception as err:
                print(f"Error parsing workflow file metadata {filename}: {err}")
                
    return results

@app.get("/config/workflows/{stage_index}")
async def get_stage_steps(stage_index: str, session_id: str = ""):
    workflow_dir = "workflow"
    if os.path.exists(workflow_dir):
        # Find the specific YAML file corresponding to this stage index
        files = [f for f in os.listdir(workflow_dir) if (f.endswith(".yaml") or f.endswith(".yml")) and f.startswith(f"{stage_index}.")]
        if files:
            filename = files[0]
            file_path = os.path.join(workflow_dir, filename)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    yaml_content = f.read()
                parsed = parse_lightweight_yaml(yaml_content)
                
                # Fetch thread-safe session context or fallback default
                ctx = session_configs.get(session_id, default_config_context)
                
                # Render each script_content in workflow_steps using JINJA2 Template!
                rendered_steps = []
                for step in parsed.get("workflow_steps", []):
                    raw_content = step.get("script_content", "")
                    if raw_content:
                        try:
                            # Compile Jinja2 Template on-demand!
                            template = Template(raw_content)
                            # Render using both "test_dict" namespace and flat variables
                            rendered_content = template.render(
                                test_dict=ctx,
                                **ctx
                            )
                            step["script_content"] = rendered_content
                        except Exception as j_err:
                            print(f"Jinja2 rendering error on step '{step.get('steps_name')}': {j_err}")
                    rendered_steps.append(step)
                
                return {
                    "stage_index": stage_index,
                    "workflow_name": parsed.get("workflow_name", "Stage"),
                    "workflow_steps": rendered_steps
                }
            except Exception as err:
                print(f"Error parsing workflow steps for {stage_index}: {err}")
                
    return {"stage_index": stage_index, "workflow_name": "Stage", "workflow_steps": []}

# Background thread runner to continuously read from the blocking SSH channel 
# and safely push to the asyncio queue using loop.call_soon_threadsafe()

@app.post("/config/merge-product")
async def merge_product_config(req: ProductMergeRequest):
    master_config_path = "config/vt_perf_auto.toml"
    machine_config_path = f"config/machine_config/{req.host}.toml"
    
    final_config_dict = {}
    
    # 1. Parse Master TOML
    if os.path.exists(master_config_path):
        try:
            with open(master_config_path, "rb") as f:
                final_config_dict = tomllib.load(f)
        except Exception as err:
            print(f"Error parsing master TOML: {err}")
            
    # 2. Parse and Overwrite/Merge Machine TOML (on top of master config!)
    if os.path.exists(machine_config_path):
        try:
            with open(machine_config_path, "rb") as f:
                machine_dict = tomllib.load(f)
            final_config_dict = deep_merge(copy.deepcopy(final_config_dict), machine_dict)
        except Exception as err:
            print(f"Error parsing machine TOML for {req.host}: {err}")
            
    # 3. Inject product info metrics
    if "PRODUCT" not in final_config_dict:
        final_config_dict["PRODUCT"] = {}
    final_config_dict["PRODUCT"]["prd_info"] = {
        "product": req.product,
        "milestone": req.milestone,
        "build_ver": 'Build%s' % req.build_ver,
        "build_num": req.build_ver
    }
    
    # 4. Create context mapping for {placeholder} replacement
    context = flatten_dict(final_config_dict)
    context.update({
        "host": req.host,
        "hostname": req.host,
        "product": req.product,
        "milestone": req.milestone,
        "build_ver": req.build_ver,
        "buildVer": req.build_ver,
        "remote_ip": req.host
    })
    
    # 5. Recursively resolve placeholders
    final_config_dict = resolve_placeholders(final_config_dict, context)
    
    # Save/Update the thread-safe session dictionary so that users are completely isolated!
    session_configs[req.session_id] = final_config_dict
    
    # Print the resulting merged integrated dictionary directly to the backend terminal stdout console with forced flush!
    import sys
    print("\n" + "="*65, flush=True)
    print(f"INTEGRATED PRODUCT MERGE CONFIGURATION FOR SUT '{req.host}' (TOML DICT):", flush=True)
    print("="*65, flush=True)
    pprint.pprint(final_config_dict)
    print("="*65 + "\n", flush=True)
    sys.stdout.flush()
    
    return final_config_dict

def ssh_to_queue_reader(channel, async_queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
    try:
        while True:
            # recv() blocks until there is data to read
            data = channel.recv(4096)
            if not data:
                # EOF: Channel closed by remote host. Put None as sentinel.
                loop.call_soon_threadsafe(async_queue.put_nowait, None)
                break
            
            # Put bytes directly into the asyncio queue in a thread-safe manner
            loop.call_soon_threadsafe(async_queue.put_nowait, data)
    except Exception as e:
        print(f"SSH reader thread exception: {e}")
        loop.call_soon_threadsafe(async_queue.put_nowait, None)

@app.websocket("/")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    ssh_client = None
    ssh_channel = None
    async_queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    write_task = None
    
    # Nested async task that runs on the main thread's event loop.
    # Reads from the asyncio queue sequentially and sends over WebSocket safely.
    # This completely avoids ASGI state collisions.
    async def write_to_websocket_loop():
        try:
            while True:
                data = await async_queue.get()
                if data is None:
                    # EOF from remote host
                    payload = json.dumps({"action": "status", "data": "\r\nConnection closed by remote host.\r\n"})
                    await websocket.send_text(payload)
                    await websocket.close()
                    break
                
                # Decode bytes to string
                text = data.decode('utf-8', errors='replace')
                payload = json.dumps({"action": "data", "data": text})
                await websocket.send_text(payload)
        except Exception as err:
            print(f"WS write task exception: {err}")
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    try:
        while True:
            # Wait for client packets
            message = await websocket.receive_text()
            parsed = json.loads(message)
            action = parsed.get("action")
            
            if action == "connect":
                if ssh_channel is not None:
                    await websocket.send_text(json.dumps({
                        "action": "error", 
                        "data": "Already connected to an SSH session."
                    }))
                    continue
                
                host = parsed.get("host")
                port = int(parsed.get("port", 22))
                username = parsed.get("username")
                password = parsed.get("password")
                cols = int(parsed.get("cols", 80))
                rows = int(parsed.get("rows", 24))
                
                # Retrieve new product, milestone, and build_ver parameters
                try:
                    # Initialize Paramiko SSH client
                    ssh_client = paramiko.SSHClient()
                    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    
                    # Connect to SSH host
                    ssh_client.connect(
                        hostname=host,
                        port=port,
                        username=username,
                        password=password,
                        timeout=10
                    )
                    
                    # Set TCP Keep-alive to preserve the SSH session indefinitely
                    transport = ssh_client.get_transport()
                    if transport:
                        transport.set_keepalive(30)
                    
                    # Invoke shell channel with precise rows and cols
                    ssh_channel = ssh_client.invoke_shell(
                        term="xterm-color",
                        width=cols,
                        height=rows
                    )
                    
                    # Start the websocket writer coroutine in the background of event loop
                    write_task = asyncio.create_task(write_to_websocket_loop())
                    
                    # Notify frontend that connection is established
                    await websocket.send_text(json.dumps({"action": "connected"}))
                    
                    # Write physical connection welcome notification directly onto the terminal screen
                    welcome_alert = f"\r\n\x1b[36m[System] Initializing SUT SSH Terminal session on '{host}'...\x1b[0m\r\n\r\n"
                    await websocket.send_text(json.dumps({"action": "status", "data": welcome_alert}))
                    
                    # Start background thread to read from blocking SSH channel
                    reader_thread = threading.Thread(
                        target=ssh_to_queue_reader,
                        args=(ssh_channel, async_queue, loop),
                        daemon=True
                    )
                    reader_thread.start()
                    
                except Exception as err:
                    print(f"SSH Auth/Conn failure: {err}")
                    await websocket.send_text(json.dumps({
                        "action": "error",
                        "data": f"SSH connection error: {str(err)}"
                    }))
                    await websocket.close()
                    break
                    
            elif action == "data":
                if ssh_channel:
                    ssh_channel.send(parsed.get("data", ""))
                    
            elif action == "resize":
                if ssh_channel:
                    cols = int(parsed.get("cols", 80))
                    rows = int(parsed.get("rows", 24))
                    ssh_channel.resize_pty(width=cols, height=rows)
                    
    except WebSocketDisconnect:
        print("WebSocket connection disconnected cleanly.")
    except Exception as e:
        print(f"WebSocket endpoint exception: {e}")
    finally:
        # Cancel the websocket writer task if it's running
        if write_task:
            write_task.cancel()
        # Resource cleanup
        if ssh_channel:
            try:
                ssh_channel.close()
            except Exception:
                pass
        if ssh_client:
            try:
                ssh_client.close()
            except Exception:
                pass

# Mount static files on root fallback (Must reside after websocket path for proper routing priority)
app.mount("/", StaticFiles(directory="public", html=True), name="public")

if __name__ == "__main__":
    import uvicorn
    # Start the server on port 3000 to maintain direct parity with original server
    uvicorn.run(app, host="0.0.0.0", port=3000)
