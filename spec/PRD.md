# Product Requirements Document (PRD) & Technical Specification: VT Performance Auto-Tuning Dashboard

## 1. Executive Summary & Objective
The **VT (Virtualization Technology) Performance Auto-Tuning Dashboard** is a unified, real-time web application designed to configure, deploy, and auto-tune system virtualization environments and run performance benchmarks on target SUTs (Systems Under Test).

### Core Goals:
*   **Centralized SUT Management:** Eliminate manual console tasks by executing automated SUT preparation, hypervisor deployment, and benchmark runs via an interactive terminal.
*   **Dynamic TOML Configuration:** Leverage a master TOML matched with host-specific overrides to compile and resolve parameters (such as repository URLs, VM sizes, networks, and disk specifications).
*   **Real-time Interaction:** Provide an integrated, low-latency, xterm.js-powered web terminal connected to the SUT over persistent SSH tunnels.
*   **Two-Level JIT Workflow Execution:** Parse and render multi-stage YAML workflows dynamically, compiling deployment scripts on the fly with host configuration metrics before viewing or running them.

---

## 2. Technical Stack
The application is structured as a single-repo, low-footprint application utilizing lightweight, zero-compilation web interfaces and asynchronous backend microservices:

| Layer | Technology | Key Components / Versioning |
| :--- | :--- | :--- |
| **Frontend** | Vanilla HTML5 / CSS3 / ES6 | Modern custom CSS variables design, CSS background-glow gradients |
| **Terminal Core** | xterm.js | Asynchronous xterm.js client for rendering low-latency SUT output |
| **Backend API** | FastAPI (Python 3.11+) | Asynchronous routing, WebSocket endpoint, Pydantic request verification |
| **Template Engine** | Jinja2 | Dynamic script rendering with variable scopes and namespace support |
| **SSH Client** | Paramiko | Multi-threaded SSH channel manager with TCP keep-alive (30s intervals) |
| **Config Loader** | tomllib (Standard Library) | Safe native Python binary parsing of TOML files |

---

## 3. Directory Structure
```text
VT_Perf_Workflow_Stepinto/
├── server.py                  # Core backend FastAPI application & API endpoints
├── config/
│   ├── vt_perf_auto.toml       # Master configuration database file
│   └── machine_config/         # Host-specific hardware/network/software overrides
│       ├── 1u-perf02.toml
│       ├── 2u-perf02.toml
│       ├── ph045.toml
│       ├── ph047.toml
│       ├── ph051.toml
│       ├── template.toml
│       ├── vh018.toml
│       └── vh019.toml
├── public/                    # Frontend static assets
│   ├── index.html             # Main application layout and forms
│   ├── style.css              # Custom styling sheet (Vanilla CSS, Fira Code font)
│   ├── app.js                 # Frontend application state, workflows & WebSocket lifecycle
│   └── favicon.svg            # Site vector brand icon
└── workflow/                  # Multi-stage shell script YAML pipeline files
    ├── 01.SUT_Environment_Prepare.yaml
    ├── 02.Hypervisor_VM_Deploy.yaml
    ├── 03.Benchmark_Execution.yaml
    └── 04.System_Performance_Tracing.yaml
```

---

## 4. System Architecture & Flow Diagram

The application leverages a thread-safe session registry to achieve **Concurrency Isolation** for multiple browser tabs. All operations are non-blocking:

```text
+-----------------------------------------------------------------------------------------------------------------+
|                                                   BROWSER CLIENT                                                |
|                                                                                                                 |
|    +-----------------------+      (HTTP POST /config/merge-product)        +--------------------------------+   |
|    | SUT & Product Config  | --------------------------------------------> |                                |   |
|    +-----------------------+                                               |      FastAPI Backend Server    |   |
|                                                                            |                                |   |
|    +-----------------------+      (HTTP GET /config/workflows/01?sess_id)  | 1. Parses Master & SUT TOMLs   |   |
|    |  Workflow Accordion   | <-------------------------------------------- | 2. Dynamic Region Detection    |   |
|    +-----------------------+                                               | 3. Resolves self-references    |   |
|                                                                            | 4. Compiles Jinja2 scripts     |   |
|    +-----------------------+             (WebSocket API /)                 | 5. Isolated Session Storage    |   |
|    |   xterm.js Terminal   | <===========================================> |                                |   |
|    +-----------------------+                                               +--------------------------------+   |
+-----------------------------------------------------------------------------------------||----------------------+
                                                                                          ||
                                                                                          ||  (Persistent SSH Tunnel)
                                                                                          \/
                                                                             +--------------------------------+
                                                                             |      Target SUT Hardware       |
                                                                             +--------------------------------+
```

---

## 5. Backend API Specification

### 5.1 Rest Endpoints

#### 1. Fetch Pipeline Stages Metadata
*   **Endpoint:** `GET /config/workflows`
*   **Description:** Scans the `./workflow` folder, JIT-parses the metadata of all YAML files alphabetically, and returns stage indices and pipeline names.
*   **Query Parameters:**
    *   `session_id` (string, optional)
    *   `hostname` (string, optional) - If provided, dynamically updates default SUT context.
*   **Response Payload (`200 OK`):**
```json
[
  {
    "file_name": "01.SUT_Environment_Prepare.yaml",
    "stage_index": "01",
    "workflow_name": "SUT Environment Prepare"
  },
  {
    "file_name": "02.Hypervisor_VM_Deploy.yaml",
    "stage_index": "02",
    "workflow_name": "Hypervisor & VM Deploy"
  }
]
```

#### 2. Fetch Compiled Workflow Steps of a Stage
*   **Endpoint:** `GET /config/workflows/{stage_index}`
*   **Description:** Parses the workflow YAML file matching `{stage_index}`. Uses the session-specific merged TOML dictionary to compile step descriptions and bash script blocks on-the-fly.
*   **Query Parameters:**
    *   `session_id` (string, required)
    *   `hostname` (string, optional) - Dynamically updates default SUT context.
*   **Response Payload (`200 OK`):**
```json
{
  "stage_index": "01",
  "workflow_name": "SUT Environment Prepare",
  "workflow_steps": [
    {
      "steps_name": "Add repository[s]",
      "script_name": "add_repos.sh",
      "script_content": "#!/bin/bash\nzypper ar http://mirror.suse.asia/ibs/QA:/Head/SLE-15-SP7/ qa_head",
      "script_instroduction": "The step is to install packages for virtualization and host monitor."
    }
  ]
}
```

#### 3. Merge Product Configurations
*   **Endpoint:** `POST /config/merge-product`
*   **Description:** Dynamically parses Master TOML and Host TOML, injects the request product criteria, processes regional mappings, executes recursive self-reference replacement, and stores the resulting dict into session-isolated storage.
*   **Request Schema (Pydantic model):**
```json
{
  "host": "ph047",
  "product": "sles-15-sp7",
  "milestone": "GM",
  "build_ver": "Build44.4",
  "session_id": "sess_8n2f9sa_1720000000000"
}
```
*   **Response Payload (`200 OK`):** Returns the fully-resolved merged integrated configuration dictionary (deep JSON representation).

---

### 5.2 WebSocket Endpoint
*   **Endpoint:** `WS /`
*   **Protocol:** WebSocket (`ws://` or `wss://`)
*   **Description:** Operates as a duplex bridge connecting the frontend xterm.js terminal with the target SUT over a persistent, interactive Paramiko SSH channel.
*   **Client Message Schema (JSON String):**
    *   **Connection request:**
```json
{ "action": "connect", "host": "ph047", "port": 22, "username": "root", "password": "...", "cols": 120, "rows": 30 }
```
    *   **Interactive Terminal Shell Input:**
```json
{ "action": "data", "data": "zypper install -y sysstat\r" }
```
    *   **Resizing dimensions:**
```json
{ "action": "resize", "cols": 140, "rows": 35 }
```
*   **Server Stream Messages (JSON String):**
    *   **Terminal Data output:**
```json
{ "action": "data", "data": "[System] Initializing SUT SSH Terminal session...\r\n" }
```
    *   **Status Update notifications:**
```json
{ "action": "status", "data": "\r\nConnection closed by remote host.\r\n" }
```

---

## 6. Dynamic Configuration & Placeholder Resolution Core

A key highlight of the application is the **Dynamic Variable Resolution and Self-Reference Engine** implemented in the backend:

### 6.1 Custom Variable Wrappers
To handle complex nesting, indexing, and dot-notation on strings and dictionaries, the system employs custom structures:
1.  **`AttrDict(dict)`**: Allows dictionary keys to be accessed using both Python index syntax (`d['key']`) and dot notation (`d.key`).
2.  **`AttrStr(str)`**: A string wrapper supporting arbitrary attribute lookup and indexing. If the TOML uses `{guest_prd_url.startup_repo}` or `{guest_prd_url[startup_repo]}` but `guest_prd_url` is just a string (as is the case in SLES 15 SP7), the wrapper gracefully intercepts and returns itself instead of throwing a lookup or attribute error.

```python
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
```

### 6.2 Recursive Inner-Out Resolution (`resolve_nested_braces`)
The system resolves placeholders (both in TOML parameters and raw YAML bash blocks) from the inside out to allow nested evaluations (such as resolving `{ test_dict.PRODUCT.qa_repo_map.{ product_ver } }`):

```python
def resolve_nested_braces(text, context, local_scope=None):
    if not isinstance(text, str) or "{" not in text or "}" not in text:
        return text
    
    formatter_map = LazyFormatterMap(context, local_scope or {}, resolve_placeholders)
    limit = 100
    while limit > 0:
        # Matches innermost curly braces: e.g. { product_ver }
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
            # Hide braces to avoid infinite lookup loop
            text = text[:match.start()] + f"__LEFT_BRACE__{inner_content}__RIGHT_BRACE__" + text[match.end():]
        else:
            text = text[:match.start()] + str(resolved_val) + text[match.end():]
        limit -= 1
        
    text = text.replace("__LEFT_BRACE__", "{").replace("__RIGHT_BRACE__", "}")
    return text
```

---

## 7. Frontend Interface Features
The application interface is a sleek, black-themed dashboard styled around deep space aesthetics (Vanilla CSS):

*   **Glassmorphic Config Cards:** Side-by-side cards utilizing absolute sizing (`flex: 1`), glowing ambient backdrops, custom inputs, and dynamic dropdown options.
*   **Collapsible Workflow Accordion:** Level-1 stages list and JIT Level-2 steps that dynamically query, compile, and present rendered script files on-click.
*   **Splitted Live Shell Workspace:** Includes a script code viewer on the left (showing compiled deployment code with highlighted formatting) and the custom `xterm.js` terminal on the right.
*   **Terminal Interaction Shortcuts:** A custom CLI quick-command input bar with command memory history store (`cmdHistory`) supporting quick-firing lines onto the live remote SUT terminal directly.

---

## 8. Success Criteria & Verification Boundaries

To complete validation, the following behaviors must be maintained:

```text
+----------------------------------------------------------------------------+
| ALWAYS DO                                                                  |
| - Verify SUT regional IP grid matches (10.200.x.x -> ZH, 10.145.x.x -> CZ) |
| - Isolate configurations thread-safely using random Session IDs            |
| - Pre-resolve nested braces before launching Jinja2 compilation            |
+----------------------------------------------------------------------------+
| ASK FIRST                                                                  |
| - Mutating schemas or adding packages to master vt_perf_auto.toml          |
| - Elevating Paramiko SSH keep-alive frequencies below 30s                  |
+----------------------------------------------------------------------------+
| NEVER DO                                                                   |
| - Hardcode region variables or master/host file paths in code loops        |
| - Commit local credentials, passwords, or test keys to origin repository    |
+----------------------------------------------------------------------------+
```
