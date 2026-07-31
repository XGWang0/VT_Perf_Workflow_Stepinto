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
import re

app = FastAPI()

class ProductMergeRequest(BaseModel):
    host: str
    product: str
    milestone: str
    build_ver: str
    product_img: str = ""
    
    guest_product: str
    guest_milestone: str
    guest_build_ver: str
    guest_product_img: str = ""
    
    session_id: str

class AttrDict(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for k, v in self.items():
            if isinstance(v, dict):
                self[k] = AttrDict(v)
    def __getattr__(self, name):
        if name in self:
            return self[name]
        raise AttributeError(f"No attribute {name}")
    def __str__(self):
        # Default string representation: use "vm" value if present, otherwise first value, or empty string.
        if "vm" in self:
            return str(self["vm"])
        return next(iter(self.values())) if self else ""
    def __format__(self, format_spec):
        return self.__str__()

class AttrStr(str):
    def __getattr__(self, name):
        return AttrStr(self)
    def __getitem__(self, key):
        if isinstance(key, str):
            return AttrStr(self)
        return super().__getitem__(key)

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

class LazyFormatterMap(dict):
    def __init__(self, context, local_scope, resolve_func):
        self.context = context
        self.local_scope = local_scope
        self.resolve_func = resolve_func
        self.cache = {}
        
    def __getitem__(self, key):
        if key in self.cache:
            return self.cache[key]
            
        val = None
        if key in self.local_scope:
            val = self.local_scope[key]
        elif key in self.context:
            val = self.context[key]
            
        if val is not None:
            if isinstance(val, str) and "{" in val and "}" in val:
                # Prevent infinite recursion if a key references itself
                sub_local = dict(self.local_scope)
                sub_local.pop(key, None)
                resolved = self.resolve_func(val, self.context, sub_local)
                self.cache[key] = resolved
                return resolved
            else:
                self.cache[key] = val
                return val
                
        return f"{{{key}}}"
        
    def __missing__(self, key):
        return f"{{{key}}}"

def resolve_nested_braces(text, context, local_scope=None):
    if not isinstance(text, str) or "{" not in text or "}" not in text:
        return text
        
    formatter_map = LazyFormatterMap(context, local_scope or {}, resolve_placeholders)
    
    limit = 100 # prevent infinite loops
    while limit > 0:
        # Find the innermost braces block (contains no other '{' or '}')
        match = re.search(r"\{([^{}]+)\}", text)
        if not match:
            break
            
        full_placeholder = match.group(0)
        inner_content = match.group(1).strip()
        
        temp_str = f"{{{inner_content}}}"
        try:
            resolved_val = temp_str.format_map(formatter_map)
        except Exception:
            resolved_val = full_placeholder
            
        if resolved_val == temp_str:
            # Hide braces to avoid infinite loop
            text = text[:match.start()] + f"__LEFT_BRACE__{inner_content}__RIGHT_BRACE__" + text[match.end():]
        else:
            text = text[:match.start()] + str(resolved_val) + text[match.end():]
            
        limit -= 1
        
    text = text.replace("__LEFT_BRACE__", "{").replace("__RIGHT_BRACE__", "}")
    return text

def resolve_placeholders(item, context, local_scope=None):
    if local_scope is None:
        local_scope = {}
        
    if isinstance(item, dict):
        new_local_scope = dict(local_scope)
        for k, v in item.items():
            if isinstance(v, (str, int, float, bool)):
                new_local_scope[k] = str(v)
                
        resolved_dict = {}
        for k, v in item.items():
            resolved_dict[k] = resolve_placeholders(v, context, new_local_scope)
        return resolved_dict
        
    elif isinstance(item, list):
        return [resolve_placeholders(i, context, local_scope) for i in item]
        
    elif isinstance(item, str):
        return resolve_nested_braces(item, context, local_scope)
    else:
        return item

# Concurrency Protection: Thread-safe session configs registry to isolate multiple users.
# Keys are front-end generated unique Session IDs; values are their deep-merged TOML dicts.
session_configs = {}

# Symmetrical fallback context used on boot/startup or if a session hasn't merged yet
default_config_context = {}

def load_default_config_context(
    hostname="ph047",
    product_ver="sles-15-sp7",
    milestone="GM",
    build_ver="Build44.4",
    build_num="44.4",
    product_img="",
    
    guest_product_ver="sles-15-sp7",
    guest_milestone="GM",
    guest_build_ver="Build44.4",
    guest_build_num="44.4",
    guest_product_img=""
):
    global default_config_context
    master_config_path = "config/vt_perf_auto.toml"
    machine_config_path = f"config/machine_config/{hostname}.toml"
    
    if not os.path.exists(machine_config_path):
        # Fall back if path does not exist
        machine_config_path = "config/machine_config/ph047.toml"
        hostname = "ph047"
        
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
            print(f"Error parsing machine TOML {hostname} during boot: {err}")
            
    # Determine Region
    region = "ZH"
    phy_machine = final_config_dict.get("PHYSICAL_MACHINE", {})
    ip_map = phy_machine.get("phy_machine_ip_address", {})
    ip_addr = ip_map.get(hostname, "")
    if ip_addr.startswith("10.145") or ip_addr.startswith("10.146") or "perf" in hostname:
        region = "CZ"
        
    # Helper to resolve product URL
    def get_product_url(p_ver, custom_img):
        if custom_img:
            return AttrStr(custom_img)
        prd_url_map = final_config_dict.get("PRODUCT", {}).get("prd_url_map", {})
        prd_url_entry = prd_url_map.get(p_ver)
        if isinstance(prd_url_entry, str):
            return AttrStr(prd_url_entry)
        elif isinstance(prd_url_entry, dict):
            regional_entry = prd_url_entry.get(region)
            if regional_entry:
                return AttrStr(regional_entry) if isinstance(regional_entry, str) else AttrDict(regional_entry)
            else:
                first_val = next(iter(prd_url_entry.values())) if prd_url_entry else ""
                return AttrStr(first_val) if isinstance(first_val, str) else AttrDict(first_val)
        return ""

    # Helper to resolve autoyast URL
    def get_autoyast_url(p_ver):
        autoyast_url_map = final_config_dict.get("VIRTUAL_MACHINE", {}).get("autoyast_url", {})
        autoyast_entry = autoyast_url_map.get(p_ver)
        if isinstance(autoyast_entry, str):
            return AttrStr(autoyast_entry)
        elif isinstance(autoyast_entry, dict):
            return AttrDict(autoyast_entry)
        return ""

    # Calculate Host and Guest URLs
    host_prd_url = get_product_url(product_ver, product_img)
    guest_prd_url = get_product_url(guest_product_ver, guest_product_img)
    auto_yast = get_autoyast_url(guest_product_ver)

    # 3. Inject product info metrics
    if "PRODUCT" not in final_config_dict:
        final_config_dict["PRODUCT"] = {}
        
    final_config_dict["PRODUCT"]["prd_info"] = {
        "product_ver": product_ver,
        "milestone": milestone,
        "build_ver": 'Build%s' % build_num if not build_ver.startswith('Build') else build_ver,
        "build_num": build_num
    }
    
    final_config_dict["PRODUCT"]["guest_prd_info"] = {
        "product_ver": guest_product_ver,
        "milestone": guest_milestone,
        "build_ver": 'Build%s' % guest_build_num if not guest_build_ver.startswith('Build') else guest_build_ver,
        "build_num": guest_build_num
    }
        
    # Create temp context to resolve Host URLs
    host_temp_context = {
        "host": hostname,
        "hostname": hostname,
        "product_ver": product_ver,
        "milestone": milestone,
        "build_ver": build_ver,
        "buildVer": build_ver,
        "build_num": build_num,
        "remote_ip": hostname,
        "password": final_config_dict.get("VIRTUAL_MACHINE", {}).get("password", "nots3cr3t")
    }

    # Create temp context to resolve Guest URLs
    guest_temp_context = {
        "host": hostname,
        "hostname": hostname,
        "product_ver": guest_product_ver,
        "milestone": guest_milestone,
        "build_ver": guest_build_ver,
        "buildVer": guest_build_ver,
        "build_num": guest_build_num,
        "remote_ip": hostname,
        "password": final_config_dict.get("VIRTUAL_MACHINE", {}).get("password", "nots3cr3t")
    }
    
    host_prd_url = resolve_placeholders(host_prd_url, host_temp_context)
    guest_prd_url = resolve_placeholders(guest_prd_url, guest_temp_context)
    auto_yast = resolve_placeholders(auto_yast, guest_temp_context)
    
    # Wrap in AttrDict/AttrStr
    if isinstance(host_prd_url, dict):
        host_prd_url = AttrDict(host_prd_url)
    if isinstance(guest_prd_url, dict):
        guest_prd_url = AttrDict(guest_prd_url)
    if isinstance(auto_yast, dict):
        auto_yast = AttrDict(auto_yast)
        
    # Inject hostname into PHYSICAL_MACHINE
    if "PHYSICAL_MACHINE" not in final_config_dict:
        final_config_dict["PHYSICAL_MACHINE"] = {}
    final_config_dict["PHYSICAL_MACHINE"]["hostname"] = hostname

    # Inject root-level variables for template contexts (both Python and Jinja2)
    final_config_dict["host"] = hostname
    final_config_dict["hostname"] = hostname
    final_config_dict["product_ver"] = product_ver
    final_config_dict["milestone"] = milestone
    final_config_dict["build_ver"] = build_ver
    final_config_dict["build_num"] = build_num
    final_config_dict["remote_ip"] = hostname
    
    final_config_dict["guest_product_ver"] = guest_product_ver
    final_config_dict["guest_milestone"] = guest_milestone
    final_config_dict["guest_build_ver"] = guest_build_ver
    final_config_dict["guest_build_num"] = guest_build_num
    
    final_config_dict["host_prd_url"] = host_prd_url
    final_config_dict["guest_prd_url"] = guest_prd_url
    final_config_dict["auto_yast"] = auto_yast

    # 4. Create context mapping for {placeholder} replacement
    context = flatten_dict(final_config_dict)
    context.update({
        "host": hostname,
        "hostname": hostname,
        "product_ver": product_ver,
        "milestone": milestone,
        "build_ver": build_ver,
        "buildVer": build_ver,
        "build_num": build_num,
        "remote_ip": hostname,
        
        "guest_product_ver": guest_product_ver,
        "guest_milestone": guest_milestone,
        "guest_build_ver": guest_build_ver,
        "guest_build_num": guest_build_num,
        
        "host_prd_url": host_prd_url,
        "guest_prd_url": guest_prd_url,
        "auto_yast": auto_yast,
        "password": final_config_dict.get("VIRTUAL_MACHINE", {}).get("password", "nots3cr3t")
    })
    
    # 5. Recursively resolve placeholders
    final_config_dict = resolve_placeholders(final_config_dict, context)
    
    # Store into fallback context!
    default_config_context.clear()
    default_config_context.update(final_config_dict)

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
async def get_dynamic_workflows(session_id: str = "", hostname: str = None, host: str = None):
    h = hostname or host
    if h:
        load_default_config_context(hostname=h)
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

def preprocess_template_includes(text):
    """
    Preprocess any JINJA2 {% include 'filename' %} statements in raw script content by
    reading the file and replacing the statement inline. This allows the backend to support
    modular macros/includes without changing the root Template compilation context.
    """
    pattern = r'{%\s*include\s+[\'"]([^\'"]+)[\'"]\s*%}'
    for _ in range(5):  # Limit nested includes to prevent infinite cycles
        matches = re.findall(pattern, text)
        if not matches:
            break
        for filename in matches:
            generic_regex = r'{%\s*include\s+[\'"]' + re.escape(filename) + r'[\'"]\s*%}'
            if os.path.exists(filename):
                try:
                    with open(filename, "r", encoding="utf-8") as f:
                        file_content = f.read()
                    text = re.sub(generic_regex, file_content, text)
                except Exception as e:
                    print(f"Error reading include file {filename}: {e}")
                    text = re.sub(generic_regex, "", text)
            else:
                print(f"Include file not found: {filename}")
                text = re.sub(generic_regex, "", text)
    return text

@app.get("/config/workflows/{stage_index}")
async def get_stage_steps(stage_index: str, session_id: str = "", hostname: str = None, host: str = None):
    h = hostname or host
    if h:
        load_default_config_context(hostname=h)
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
                            # Pre-process nested brace placeholders first (e.g., { test_dict.PRODUCT.qa_repo_map.{ product_ver } })
                            resolved_raw = resolve_nested_braces(raw_content, ctx)
                            # Pre-process external template includes inline!
                            resolved_raw = preprocess_template_includes(resolved_raw)
                            # Compile Jinja2 Template on-demand!
                            template = Template(resolved_raw)
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
    
    if not os.path.exists(machine_config_path):
        machine_config_path = "config/machine_config/ph047.toml"
        req.host = "ph047"
        
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
    product_ver = req.product
    if "PRODUCT" not in final_config_dict:
        final_config_dict["PRODUCT"] = {}
    final_config_dict["PRODUCT"]["prd_info"] = {
        "product_ver": product_ver,
        "milestone": req.milestone,
        "build_ver": 'Build%s' % req.build_ver if not req.build_ver.startswith('Build') else req.build_ver,
        "build_num": req.build_ver
    }
    
    # Determine Region
    region = "ZH"
    phy_machine = final_config_dict.get("PHYSICAL_MACHINE", {})
    ip_map = phy_machine.get("phy_machine_ip_address", {})
    ip_addr = ip_map.get(req.host, "")
    if ip_addr.startswith("10.145") or ip_addr.startswith("10.146") or "perf" in req.host:
        region = "CZ"
        
    # Helper to resolve product URL
    def get_product_url(p_ver, custom_img):
        if custom_img:
            return AttrStr(custom_img)
        prd_url_map = final_config_dict.get("PRODUCT", {}).get("prd_url_map", {})
        prd_url_entry = prd_url_map.get(p_ver)
        if isinstance(prd_url_entry, str):
            return AttrStr(prd_url_entry)
        elif isinstance(prd_url_entry, dict):
            regional_entry = prd_url_entry.get(region)
            if regional_entry:
                return AttrStr(regional_entry) if isinstance(regional_entry, str) else AttrDict(regional_entry)
            else:
                first_val = next(iter(prd_url_entry.values())) if prd_url_entry else ""
                return AttrStr(first_val) if isinstance(first_val, str) else AttrDict(first_val)
        return ""

    # Helper to resolve autoyast URL
    def get_autoyast_url(p_ver):
        autoyast_url_map = final_config_dict.get("VIRTUAL_MACHINE", {}).get("autoyast_url", {})
        autoyast_entry = autoyast_url_map.get(p_ver)
        if isinstance(autoyast_entry, str):
            return AttrStr(autoyast_entry)
        elif isinstance(autoyast_entry, dict):
            return AttrDict(autoyast_entry)
        return ""

    product_ver = req.product
    guest_product_ver = req.guest_product

    # Calculate Host and Guest URLs
    host_prd_url = get_product_url(product_ver, req.product_img)
    guest_prd_url = get_product_url(guest_product_ver, req.guest_product_img)
    auto_yast = get_autoyast_url(guest_product_ver)

    # 3. Inject product info metrics
    if "PRODUCT" not in final_config_dict:
        final_config_dict["PRODUCT"] = {}
        
    final_config_dict["PRODUCT"]["prd_info"] = {
        "product_ver": product_ver,
        "milestone": req.milestone,
        "build_ver": 'Build%s' % req.build_ver if not req.build_ver.startswith('Build') else req.build_ver,
        "build_num": req.build_ver
    }
    
    final_config_dict["PRODUCT"]["guest_prd_info"] = {
        "product_ver": guest_product_ver,
        "milestone": req.guest_milestone,
        "build_ver": 'Build%s' % req.guest_build_ver if not req.guest_build_ver.startswith('Build') else req.guest_build_ver,
        "build_num": req.guest_build_ver
    }
        
    # Create temp context to resolve Host URLs
    host_temp_context = {
        "host": req.host,
        "hostname": req.host,
        "product_ver": product_ver,
        "milestone": req.milestone,
        "build_ver": req.build_ver,
        "buildVer": req.build_ver,
        "build_num": req.build_ver,
        "remote_ip": req.host,
        "password": final_config_dict.get("VIRTUAL_MACHINE", {}).get("password", "nots3cr3t")
    }

    # Create temp context to resolve Guest URLs
    guest_temp_context = {
        "host": req.host,
        "hostname": req.host,
        "product_ver": guest_product_ver,
        "milestone": req.guest_milestone,
        "build_ver": req.guest_build_ver,
        "buildVer": req.guest_build_ver,
        "build_num": req.guest_build_ver,
        "remote_ip": req.host,
        "password": final_config_dict.get("VIRTUAL_MACHINE", {}).get("password", "nots3cr3t")
    }
    
    host_prd_url = resolve_placeholders(host_prd_url, host_temp_context)
    guest_prd_url = resolve_placeholders(guest_prd_url, guest_temp_context)
    auto_yast = resolve_placeholders(auto_yast, guest_temp_context)
    auto_yast = resolve_placeholders(auto_yast, guest_temp_context)
    
    # Wrap in AttrDict/AttrStr
    if isinstance(host_prd_url, dict):
        host_prd_url = AttrDict(host_prd_url)
    if isinstance(guest_prd_url, dict):
        guest_prd_url = AttrDict(guest_prd_url)
    if isinstance(auto_yast, dict):
        auto_yast = AttrDict(auto_yast)
        
    # Inject root-level variables for template contexts (both Python and Jinja2)
    final_config_dict["host"] = req.host
    final_config_dict["hostname"] = req.host
    final_config_dict["product_ver"] = product_ver
    final_config_dict["milestone"] = req.milestone
    final_config_dict["build_ver"] = req.build_ver
    final_config_dict["build_num"] = req.build_ver
    final_config_dict["remote_ip"] = req.host
    
    final_config_dict["guest_product_ver"] = guest_product_ver
    final_config_dict["guest_milestone"] = req.guest_milestone
    final_config_dict["guest_build_ver"] = req.guest_build_ver
    final_config_dict["guest_build_num"] = req.guest_build_ver
    
    final_config_dict["host_prd_url"] = host_prd_url
    final_config_dict["guest_prd_url"] = guest_prd_url
    final_config_dict["auto_yast"] = auto_yast

    # 4. Create context mapping for {placeholder} replacement
    context = flatten_dict(final_config_dict)
    context.update({
        "host": req.host,
        "hostname": req.host,
        "product_ver": product_ver,
        "milestone": req.milestone,
        "build_ver": req.build_ver,
        "buildVer": req.build_ver,
        "build_num": req.build_ver,
        "remote_ip": req.host,
        
        "guest_product_ver": guest_product_ver,
        "guest_milestone": req.guest_milestone,
        "guest_build_ver": req.guest_build_ver,
        "guest_build_num": req.guest_build_ver,
        
        "host_prd_url": host_prd_url,
        "guest_prd_url": guest_prd_url,
        "auto_yast": auto_yast,
        "password": final_config_dict.get("VIRTUAL_MACHINE", {}).get("password", "nots3cr3t")
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
                        "data": f"Timeout connection, please try again later..." if 'timed out' in str(err) else f"SSH connection error: {str(err)}"
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
