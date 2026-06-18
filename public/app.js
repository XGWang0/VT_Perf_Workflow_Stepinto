document.addEventListener('DOMContentLoaded', () => {
  const loginForm = document.getElementById('login-form');
  const terminalWrapper = document.getElementById('terminal-wrapper');
  const terminalContainer = document.getElementById('terminal');
  const closeBtn = document.getElementById('close-btn');
  const connectBtn = document.getElementById('connect-btn');
  const errorMessage = document.getElementById('error-message');
  const sessionLabel = document.getElementById('session-label');
  const terminalAnchor = document.getElementById('terminal-anchor');
  const mainContent = document.querySelector('.main-content');
  
  const hostInput = document.getElementById('host');
  const portInput = document.getElementById('port');
  const usernameInput = document.getElementById('username');
  const passwordInput = document.getElementById('password');
  const showPasswordCb = document.getElementById('show-password-cb');

  // PRODUCT EXTENSIONS references
  const productSelect = document.getElementById('product');
  const milestoneSelect = document.getElementById('milestone');
  const buildVerInput = document.getElementById('build-ver');

  // New Merge Product Config button references (Modal removed as requested)
  const mergeConfigBtn = document.getElementById('merge-config-btn');

  // Run Script button reference
  const runScriptBtn = document.getElementById('run-script-btn');

  // Shortcut Input Bar references
  const cmdInput = document.getElementById('cmd-input');
  const sendCmdBtn = document.getElementById('send-cmd-btn');

  // Workflow steps references
  const scriptTitle = document.getElementById('script-title');
  const commandText = document.getElementById('command-text');

  // Dual Status Bars references
  const connectionStatus = document.getElementById('connection-status');
  const topStatusText = connectionStatus.querySelector('.status-text');
  const lowerStatusIndicator = document.getElementById('lower-status-indicator');

  let ws = null;
  let term = null;
  let fitAddon = null;

  // Output buffer to preserve welcome/MOTD output before the console panel is opened
  let stdoutBuffer = '';

  // Command History Memory Store for the Input Bar
  let cmdHistory = [];
  let historyIndex = -1;

  // Store active selected stepId to preserve selection across dynamic config reloads!
  let activeStepId = null;

  // CONCURRENCY ISOLATION: Generate or retrieve a thread-safe Session ID for this browser tab!
  let sessionId = sessionStorage.getItem('session_id');
  if (!sessionId) {
    sessionId = 'sess_' + Math.random().toString(36).substring(2, 15) + '_' + Date.now();
    sessionStorage.setItem('session_id', sessionId);
  }

  // Default Focus
  hostInput.focus();

  // Programmatically guarantee the Run Script button starts disabled on page load
  runScriptBtn.disabled = true;
  runScriptBtn.title = 'Connect to SUT first to run this script';

  // Explicitly reset the Code Editor to its clean placeholder state on page load/refresh 
  // to completely override any browser-restored cache values or form state recovery autofill!
  scriptTitle.textContent = 'script.sh';
  commandText.value = '# Connect and click "Merge Product Config" to compile and view your customized deployment scripts here...';
  activeStepId = null; // Clear active step selection as well!

  // Dynamically load Level 1 stages list from backend with session isolation
  function loadDynamicWorkflows() {
    // Show a loading indicator inside SUT steps list
    const container = document.getElementById('dynamic-workflow-steps');
    container.innerHTML = `
      <div class="loading-workflow-indicator">
        <div class="spinner"></div>
        <span>Compiling and generating your workflow steps...</span>
      </div>
    `;

    fetch(`/config/workflows?session_id=${sessionId}`)
      .then(res => {
        if (!res.ok) throw new Error('Failed to load dynamic stages.');
        return res.json();
      })
      .then(stages => {
        renderWorkflowTree(stages);
      })
      .catch(err => {
        console.error('Workflow dynamic loading failure:', err);
        container.innerHTML = `
          <div class="alert alert-error">
            <span>Workflow stages load error: ${err.message}. Please verify backend.</span>
          </div>
        `;
      });
  }

  // Render the hierarchical collapsible 2-level steps tree (Lazy JIT Compilation with session isolation!)
  function renderWorkflowTree(stages) {
    const container = document.getElementById('dynamic-workflow-steps');
    container.innerHTML = ''; // Clear loading spinner

    if (!stages || stages.length === 0) {
      container.innerHTML = '<div class="alert alert-error">No workflow configurations detected in backend.</div>';
      return;
    }

    stages.forEach((stage, stageIdx) => {
      // 1. Create Symmetrical Stage Card (Level 1 Stage Header) - Initially Closed with "+"
      const stageCard = document.createElement('div');
      stageCard.className = 'stage-card';
      stageCard.dataset.stageIndex = stage.stage_index;
      stageCard.innerHTML = `
        <div class="stage-card-left">
          <div class="stage-card-num">${stage.stage_index}</div>
          <div class="stage-card-title">${stage.workflow_name}</div>
        </div>
        <button type="button" class="stage-toggle-btn"><span>+</span></button>
      `;

      // 2. Create Collapsible Sub-steps Container (Level 2 steps wrapper) - Empty on boot!
      const subStepsContainer = document.createElement('div');
      subStepsContainer.className = 'sub-steps-container';
      subStepsContainer.dataset.stageIndex = stage.stage_index;

      // Click Event to Toggle Collapsible Sub-steps and Lazy Load Level 2 steps JIT!
      stageCard.addEventListener('click', () => {
        const toggleBtn = stageCard.querySelector('.stage-toggle-btn span');
        const isExpanded = subStepsContainer.classList.contains('expanded');

        if (isExpanded) {
          // Collapse
          subStepsContainer.classList.remove('expanded');
          stageCard.classList.remove('active-stage');
          toggleBtn.textContent = '+';
        } else {
          // Expand: Check if we need to lazy load its Level 2 steps first!
          if (subStepsContainer.children.length === 0) {
            toggleBtn.textContent = '...'; // Visual loading spinner state
            
            // JIT fetch Level 2 steps isolated by session_id!
            fetch(`/config/workflows/${stage.stage_index}?session_id=${sessionId}`)
              .then(res => {
                if (!res.ok) throw new Error('Failed to load steps.');
                return res.json();
              })
              .then(data => {
                // Clear any leftover loading indicator
                subStepsContainer.innerHTML = '';

                // Dynamically compile and render the Level 2 step cards on demand!
                data.workflow_steps.forEach((step, stepIdx) => {
                  const stepId = `${stage.stage_index}-${stepIdx}`;

                  const stepCard = document.createElement('div');
                  stepCard.className = 'step-card';
                  stepCard.dataset.stepId = stepId;
                  stepCard.dataset.scriptName = step.script_name;
                  stepCard.dataset.scriptContent = step.script_content;

                  stepCard.innerHTML = `
                    <div class="step-number">${stage.stage_index}.${stepIdx + 1}</div>
                    <div class="step-content">
                      <h4>${step.steps_name}</h4>
                      <p>${step.script_instroduction || 'Deploy and run virtualization performance tasks.'}</p>
                    </div>
                    <div class="step-badge">${step.script_name || 'script.sh'}</div>
                  `;

                  // Sub-step Click Event: Select Level 2 step!
                  stepCard.addEventListener('click', (e) => {
                    e.stopPropagation(); // Avoid triggering parent collapse

                    // Deactivate all step cards globally
                    document.querySelectorAll('.step-card').forEach(c => c.classList.remove('active'));
                    // Activate clicked step card
                    stepCard.classList.add('active');
                    activeStepId = stepId;

                    // Instantly inject compiled command script content into Code Editor on the right!
                    scriptTitle.textContent = step.script_name || 'script.sh';
                    commandText.value = step.script_content;
                  });

                  subStepsContainer.appendChild(stepCard);
                });

                // Smoothly slide open and update indicator
                subStepsContainer.classList.add('expanded');
                stageCard.classList.add('active-stage');
                toggleBtn.textContent = '−';
                
                // If there's an active step selection from this stage we are restoring, trigger click!
                if (activeStepId && activeStepId.startsWith(stage.stage_index)) {
                  const matchCard = subStepsContainer.querySelector(`.step-card[data-step-id="${activeStepId}"]`);
                  if (matchCard) matchCard.click();
                }
              })
              .catch(err => {
                console.error(`JIT steps load error for stage ${stage.stage_index}:`, err);
                toggleBtn.textContent = '+';
                subStepsContainer.innerHTML = `<div class="alert alert-error">Failed to compile steps: ${err.message}</div>`;
              });
          } else {
            // Already loaded, simply expand!
            subStepsContainer.classList.add('expanded');
            stageCard.classList.add('active-stage');
            toggleBtn.textContent = '−';
            
            // Re-select the first card of this stage if we just expanded it
            const firstSubCard = subStepsContainer.querySelector('.step-card');
            if (firstSubCard) {
              firstSubCard.click();
            }
          }
        }
      });

      container.appendChild(stageCard);
      container.appendChild(subStepsContainer);

      // 3. Render thick glowing Stage-Level pipeline connectors between adjacent Stage Cards!
      if (stageIdx < stages.length - 1) {
        const stageConnector = document.createElement('div');
        stageConnector.className = 'stage-connector';
        stageConnector.innerHTML = `
          <div class="stage-connector-line"></div>
          <div class="stage-connector-node" title="Pipeline Stage Transition Flow">
            <svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" class="connector-arrow-icon"><polyline points="6 9 12 15 18 9"/></svg>
          </div>
          <div class="stage-connector-line"></div>
        `;
        container.appendChild(stageConnector);
      }
    });

    // Restore previous selection if it exists
    if (activeStepId) {
      const savedCard = container.querySelector(`.step-card[data-step-id="${activeStepId}"]`);
      if (savedCard) {
        // Expand parent container automatically
        const parentContainer = savedCard.closest('.sub-steps-container');
        if (parentContainer) {
          parentContainer.classList.add('expanded');
          const parentCard = container.querySelector(`.stage-card[data-stage-index="${parentContainer.dataset.stageIndex}"]`);
          if (parentCard) {
            parentCard.classList.add('active-stage');
            parentCard.querySelector('.stage-toggle-btn span').textContent = '−';
          }
        }
        savedCard.click();
        return;
      }
    }

    // Default Fallback Boot Accordion Expand: Expand first stage "01" AND automatically click its first sub-step card!
    // This ensures that the moment the workflows are compiled, the Editor is dynamically populated and NEVER remains empty!
    const firstStageCard = container.querySelector('.stage-card');
    if (firstStageCard) {
      const toggleBtn = firstStageCard.querySelector('.stage-toggle-btn span');
      const firstSubContainer = container.querySelector('.sub-steps-container');
      
      if (firstSubContainer && firstSubContainer.children.length === 0) {
        toggleBtn.textContent = '...';
        
        fetch(`/config/workflows/01?session_id=${sessionId}`)
          .then(res => res.json())
          .then(data => {
            firstSubContainer.innerHTML = '';
            data.workflow_steps.forEach((step, stepIdx) => {
              const stepId = `01-${stepIdx}`;
              const stepCard = document.createElement('div');
              stepCard.className = 'step-card';
              stepCard.dataset.stepId = stepId;
              stepCard.dataset.scriptName = step.script_name;
              stepCard.dataset.scriptContent = step.script_content;

              stepCard.innerHTML = `
                <div class="step-number">01.${stepIdx + 1}</div>
                <div class="step-content">
                  <h4>${step.steps_name}</h4>
                  <p>${step.script_instroduction || 'Deploy and run virtualization performance tasks.'}</p>
                </div>
                <div class="step-badge">${step.script_name || 'script.sh'}</div>
              `;

              stepCard.addEventListener('click', (e) => {
                e.stopPropagation();
                document.querySelectorAll('.step-card').forEach(c => c.classList.remove('active'));
                stepCard.classList.add('active');
                activeStepId = stepId;
                scriptTitle.textContent = step.script_name || 'script.sh';
                commandText.value = step.script_content;
              });

              firstSubContainer.appendChild(stepCard);
            });

            // Smoothly slide open Stage 1
            firstSubContainer.classList.add('expanded');
            firstStageCard.classList.add('active-stage');
            toggleBtn.textContent = '−';

            // Auto-click the first step card of Stage 1 to populate the Editor instantly so it is never empty!
            const firstCard = firstSubContainer.querySelector('.step-card');
            if (firstCard) {
              firstCard.click();
            }
          });
      }
    }
  }

  // NOTE: loadDynamicWorkflows() is NO LONGER called on page load! 
  // Workflows generate ONLY when the user clicks the "Merge Product Config" button!

  // Helper to scroll page container so the active terminal bottom is always visible
  function scrollPageToBottom() {
    if (mainContent && !terminalWrapper.classList.contains('hidden')) {
      mainContent.scrollTop = mainContent.scrollHeight;
    }
  }

  // Update Top Status Bar Indicator (Webpage Header)
  function updateTopStatus(state, text) {
    connectionStatus.className = `status-indicator ${state}`;
    topStatusText.textContent = text;
  }

  // Update Lower Status Bar Indicator (Console Header)
  function updateLowerStatus(state, text) {
    lowerStatusIndicator.className = `status-indicator ${state}`;
    sessionLabel.textContent = text;
  }

  // Toggle Password Visibility Listener
  showPasswordCb.addEventListener('change', () => {
    if (showPasswordCb.checked) {
      passwordInput.type = 'text'; // Reveal plain-text password
      showPasswordCb.nextElementSibling.textContent = 'Hide'; // Update text
    } else {
      passwordInput.type = 'password'; // Mask password
      showPasswordCb.nextElementSibling.textContent = 'Show'; // Update text
    }
  });

  // Handle "Merge Product Config" button click (Saves and prints TOML dictionary inside backend console)
  mergeConfigBtn.addEventListener('click', () => {
    const host = hostInput.value.trim();
    const product = productSelect.value;
    const milestone = milestoneSelect.value;
    const buildVer = buildVerInput.value.trim();

    if (!host) {
      showError('Please configure SUT Target Hostname first.');
      return;
    }

    if (!product || !milestone || !buildVer) {
      showError('Please configure product details first.');
      return;
    }

    // Set button loading state
    mergeConfigBtn.disabled = true;
    const originalText = mergeConfigBtn.querySelector('span').textContent;
    mergeConfigBtn.querySelector('span').textContent = 'Merging...';

    // Post Product Configuration parameters to the backend for deep merging, printing and thread-safe session storage
    fetch('/config/merge-product', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        host: host,
        product: product,
        milestone: milestone,
        build_ver: buildVer,
        session_id: sessionId // <--- CONCURRENCY SHIELD: Isolate multiple users!
      })
    })
      .then(res => {
        if (!res.ok) throw new Error('Failed to merge product configuration.');
        return res.json();
      })
      .then(data => {
        // Log success on the browser console
        console.log("Successfully merged product configurations on the backend and printed TOML dict:", data);
        
        // CORE ENHANCEMENT: Immediately compile, generate, and load SUT workflows on click!
        // This causes the backend to JIT-parse the templates with the newly updated variables,
        // rendering the steps list and Code Editor script contents dynamically on screen!
        const activeCard = document.querySelector('.stage-card.active-stage');
        if (activeCard) {
          const index = activeCard.dataset.stageIndex;
          // Clear subStepsContainer to force a clean, JIT re-compile on reload!
          const subContainer = document.querySelector(`.sub-steps-container[data-stage-index="${index}"]`);
          if (subContainer) {
            subContainer.innerHTML = ''; // Force JIT refresh!
          }
          activeCard.click(); // Re-trigger click to fetch fresh rendered Jinja2 scripts!
        } else {
          loadDynamicWorkflows();
        }

        // Temporarily change button text to show success feedback
        mergeConfigBtn.querySelector('span').textContent = 'Merged & Printed!';
        setTimeout(() => {
          mergeConfigBtn.querySelector('span').textContent = originalText;
        }, 1500);
      })
      .catch(err => {
        console.error('Error during product TOML merge:', err);
        showError(`Merge Config Error: ${err.message}`);
      })
      .finally(() => {
        // Restore button state
        mergeConfigBtn.disabled = false;
      });
  });

  // Show inline login error
  function showError(msg) {
    errorMessage.textContent = msg;
    errorMessage.classList.remove('hidden');
  }

  // Clear errors
  function clearError() {
    errorMessage.textContent = '';
    errorMessage.classList.add('hidden');
  }

  // Set form loading/connected states
  function setLoading(loading, isConnected = false) {
    if (loading) {
      // Disables SUT-related connection inputs
      hostInput.disabled = true;
      portInput.disabled = true;
      usernameInput.disabled = true;
      passwordInput.disabled = true;
      showPasswordCb.disabled = true; // Lock show password button as well

      // Product Config inputs REMAIN FULLY EDITABLE AND ACTIVE!
      productSelect.disabled = false;
      milestoneSelect.disabled = false;
      buildVerInput.disabled = false;

      connectBtn.disabled = true;
      if (isConnected) {
        connectBtn.querySelector('span').textContent = 'SSH Connected';
        connectBtn.querySelector('.spinner').classList.add('hidden');
        
        // Connect Success: ALWAYS activate (Enable) the Run Script button immediately!
        // The Run Script button strictly judges SSH connected state and lights up green as requested!
        runScriptBtn.disabled = false;
        runScriptBtn.title = 'Run this shell script on SUT';
      } else {
        connectBtn.querySelector('span').textContent = 'SSH Connect';
        connectBtn.querySelector('.spinner').classList.remove('hidden');
        
        // Connecting phase: Run Script remains disabled
        runScriptBtn.disabled = true;
        runScriptBtn.title = 'Connecting to SUT...';
      }
    } else {
      // Enable SUT inputs back
      hostInput.disabled = false;
      portInput.disabled = false;
      usernameInput.disabled = false;
      passwordInput.disabled = false;
      showPasswordCb.disabled = false;

      // Product Config inputs remain active
      productSelect.disabled = false;
      milestoneSelect.disabled = false;
      buildVerInput.disabled = false;

      connectBtn.disabled = false;
      connectBtn.querySelector('span').textContent = 'SSH Connect';
      connectBtn.querySelector('.spinner').classList.add('hidden');

      // Connection broken/closed: Lock back (Disable) the Run Script button to gray
      runScriptBtn.disabled = true;
      runScriptBtn.title = 'Connect to SUT first to run this script';
    }
  }

  // Initialize and open xterm.js (Highly protected with try-catch and library sanity checks)
  function initTerminal() {
    try {
      if (term) {
        term.dispose();
      }

      // Safeguard window.Terminal checks
      const TerminalConstructor = window.Terminal;
      if (!TerminalConstructor) {
        throw new Error("xterm.js library failed to load from CDN. Please check your network connection.");
      }

      term = new TerminalConstructor({
        cursorBlink: true,
        fontFamily: '"Fira Code", Courier, monospace',
        fontSize: 13, // Slightly more compact font size for embedded terminal view
        theme: {
          background: '#050507',
          foreground: '#f3f4f6',
          cursor: '#5275ff',
          selectionBackground: 'rgba(82, 117, 255, 0.3)',
          black: '#1e1e1e',
          red: '#ef4444',
          green: '#10b981',
          yellow: '#f59e0b',
          blue: '#3b82f6',
          magenta: '#8b5cf6',
          cyan: '#06b6d4',
          white: '#f3f4f6'
        }
      });

      // Safeguard FitAddon constructor resolution (supports both window.FitAddon.FitAddon and window.FitAddon UMD patterns)
      const FitAddonConstructor = window.FitAddon?.FitAddon || window.FitAddon;
      if (!FitAddonConstructor) {
        throw new Error("xterm-addon-fit library failed to load from CDN. Please check your network connection.");
      }

      fitAddon = new FitAddonConstructor();
      term.loadAddon(fitAddon);
      
      // Intercept proposeDimensions to subtract 2 rows as a reliable bottom safety cushion
      if (fitAddon && typeof fitAddon.proposeDimensions === 'function') {
        const originalPropose = fitAddon.proposeDimensions.bind(fitAddon);
        fitAddon.proposeDimensions = function() {
          const dims = originalPropose();
          if (!dims) return undefined;
          return {
            cols: dims.cols,
            rows: Math.max(2, dims.rows - 2)
          };
        };
      }

      term.open(terminalContainer);

      // Call fit() synchronously immediately to get highly accurate initial columns/rows
      try {
        fitAddon.fit();
      } catch (e) {
        console.warn('Initial fit failed:', e);
      }

      // Wait for monospace fonts to load to guarantee correct grid calculations
      document.fonts.ready.then(() => {
        if (fitAddon) {
          try {
            fitAddon.fit();
          } catch (e) {}
        }
      });
    } catch (err) {
      console.error("Error inside initTerminal:", err);
      showError(`Terminal Initialization Error: ${err.message}`);
      throw err; // Re-throw to let parent click handler catch it
    }
  }

  // Handle Form Submission (Clicking SSH Connect -> silent backend handshake, NO terminal wrapper open yet!)
  loginForm.addEventListener('submit', (e) => {
    e.preventDefault();
    clearError();

    const host = hostInput.value.trim();
    const port = parseInt(portInput.value.trim()) || 22;
    const username = usernameInput.value.trim();
    const password = passwordInput.value;
    
    // Extract Product & Milestone dropdown selections
    const product = productSelect.value;
    const milestone = milestoneSelect.value;

    // Extract build version and prepend 'Build' prefix
    const buildVerRaw = buildVerInput.value.trim();
    const buildVer = "Build" + buildVerRaw;

    if (!host || !username || !password || !product || !milestone || !buildVerRaw) {
      showError('Please configure SUT details and product metrics.');
      return;
    }

    // Set UI to loading/disabled state (connecting...)
    setLoading(true, false);
    updateTopStatus('connecting', 'Connecting...');
    updateLowerStatus('connecting', 'connecting...');

    // Connect to local WebSocket proxy
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}`;

    try {
      ws = new WebSocket(wsUrl);
    } catch (err) {
      showError(`WebSocket error: ${err.message}`);
      handleDisconnectUI();
      return;
    }

    ws.onopen = () => {
      // Send connection credentials along with Product info extensions
      ws.send(JSON.stringify({
        action: 'connect',
        host,
        port,
        username,
        password,
        cols: 80, // Default cols/rows for silent buffering
        rows: 24,
        product,
        milestone,
        buildVer
      }));
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);

        if (msg.action === 'connected') {
          // 1. Lock SUT connection inputs and set Connected text on the button
          setLoading(true, true);
          
          // 2. Set Top Status: glowing green and 'Connected: username@host'
          updateTopStatus('connected', `Connected: ${username}@${host}`);

          // 3. Set Lower Status (below): connected
          updateLowerStatus('connected', 'connected');

        } else if (msg.action === 'data') {
          if (term) {
            term.write(msg.data);
            scrollPageToBottom();
          } else {
            // Buffer standard output if the terminal window has not been opened/revealed yet!
            stdoutBuffer += msg.data;
          }
        } else if (msg.action === 'status') {
          if (term) {
            term.write(msg.data);
            scrollPageToBottom();
          } else {
            stdoutBuffer += msg.data;
          }
        } else if (msg.action === 'error') {
          if (ws && ws.readyState === WebSocket.OPEN) {
            if (term) {
              term.write(`\r\n\x1b[31m[Proxy Error] ${msg.data}\x1b[0m\r\n`);
            } else {
              stdoutBuffer += `\r\n[Proxy Error] ${msg.data}\r\n`;
            }
          } else {
            showError(msg.data);
            handleDisconnectUI();
          }
        }
      } catch (err) {
        console.error('Error handling WebSocket frame:', err);
      }
    };

    ws.onclose = () => {
      handleDisconnectUI();
    };

    ws.onerror = (err) => {
      console.error('WebSocket connection error:', err);
      if (terminalWrapper.classList.contains('hidden')) {
        showError('Could not establish connection to the backend proxy.');
      }
      handleDisconnectUI();
    };
  });

  // Handle RUN Script Button Action (OPENS the connection window directly inside the script-column!)
  runScriptBtn.addEventListener('click', () => {
    try {
      // Core check: Verify if a sub-step has been manually selected first!
      if (!activeStepId) {
        showError("You must select an automated workflow sub-step card on the left first to load and run its script on SUT!");
        return;
      }

      const scriptText = commandText.value;
      if (!scriptText) return;

      if (!ws || ws.readyState !== WebSocket.OPEN) {
        throw new Error("No active SSH session. Please connect via 'SSH Connect' first.");
      }

      // 1. Open / Reveal the SSH terminal panel directly nested below the script editor!
      terminalWrapper.classList.remove('hidden');

      // 2. If the terminal xterm.js element is not initialized yet, initialize and write buffer!
      if (!term) {
        initTerminal();
        if (stdoutBuffer) {
          term.write(stdoutBuffer);
          stdoutBuffer = ''; // Clear buffer
        }

        // Forward terminal keyboard strokes to proxy websocket
        term.onData((data) => {
          if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
              action: 'data',
              data: data
            }));
            scrollPageToBottom();
          }
        });
      }

      // 3. Trigger immediate fit computation to match the newly revealed wrapper layout inside script-column
      setTimeout(() => {
        try {
          if (fitAddon) {
            fitAddon.fit();
            ws.send(JSON.stringify({
              action: 'resize',
              cols: term.cols,
              rows: term.rows
            }));
          }

          // 4. Send the entire multi-line script content to execute inside the retained SUT session
          ws.send(JSON.stringify({
            action: 'data',
            data: scriptText + '\n'
          }));

          term.focus();
          scrollPageToBottom();
        } catch (subErr) {
          console.error("Error during script execution sequence:", subErr);
          showError(`Script Execution Error: ${subErr.message}`);
        }
      }, 50);

    } catch (err) {
      console.error("Error in Run Script click handler:", err);
      showError(`Run Script Error: ${err.message}`);
    }
  });

  // Handle Close/Disconnect Button
  closeBtn.addEventListener('click', () => {
    if (ws) {
      ws.close();
    }
    // Deep UI cleanup to hide terminal panel completely
    cleanupUI();
  });

  // Handle window resizing
  window.addEventListener('resize', () => {
    if (term && fitAddon && ws && ws.readyState === WebSocket.OPEN) {
      document.fonts.ready.then(() => {
        fitAddon.fit();
        ws.send(JSON.stringify({
          action: 'resize',
          cols: term.cols,
          rows: term.rows
        }));
        scrollPageToBottom();
      });
    }
  });

  // Handle Execute Command via Shortcut Input Bar
  function executeCommand() {
    const cmd = cmdInput.value.trim(); // Get command
    if (!cmd) return; // Do nothing if empty

    if (ws && ws.readyState === WebSocket.OPEN) {
      // Send the command text appended with a carriage return \r to execute it immediately
      ws.send(JSON.stringify({
        action: 'data',
        data: cmd + '\r'
      }));

      // Store in memory command history if different from the last entry
      if (cmdHistory.length === 0 || cmdHistory[cmdHistory.length - 1] !== cmd) {
        cmdHistory.push(cmd);
        if (cmdHistory.length > 100) {
          cmdHistory.shift();
        }
      }
      historyIndex = cmdHistory.length;

      cmdInput.value = ''; // Clear input field
      cmdInput.focus(); // Re-focus the input
      scrollPageToBottom();
    }
  }

  sendCmdBtn.addEventListener('click', executeCommand);
  
  // Custom Keydown Listener for execution (Enter) and command history navigation (Up / Down Arrows)
  cmdInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      executeCommand();
    } else if (e.key === 'ArrowUp') {
      if (cmdHistory.length > 0 && historyIndex > 0) {
        e.preventDefault();
        historyIndex--;
        cmdInput.value = cmdHistory[historyIndex];
        setTimeout(() => {
          cmdInput.selectionStart = cmdInput.selectionEnd = cmdInput.value.length;
        }, 0);
      }
    } else if (e.key === 'ArrowDown') {
      if (historyIndex < cmdHistory.length - 1) {
        e.preventDefault();
        historyIndex++;
        cmdInput.value = cmdHistory[historyIndex];
      } else if (historyIndex === cmdHistory.length - 1) {
        e.preventDefault();
        historyIndex++;
        cmdInput.value = '';
      }
    }
  });

  // Handle connection drops or breaks elegantly without collapsing the terminal window
  function handleDisconnectUI() {
    ws = null;
    setLoading(false);
    
    // Top Status: Disconnected
    updateTopStatus('disconnected', 'Disconnected');

    // Lower Status: connect broken! (glowing red)
    updateLowerStatus('disconnected', 'connect broken');

    if (term) {
      term.write('\r\n\x1b[31m[Status] Connection broken. Terminal session terminated.\x1b[0m\r\n');
    }
  }

  // Deep cleanup called ONLY on close button click to completely wipe state and hide terminal panel
  function cleanupUI() {
    ws = null;
    setLoading(false);
    passwordInput.value = ''; // Clean password
    cmdInput.value = ''; // Clean shortcut input
    cmdHistory = []; // Reset history
    historyIndex = -1;
    stdoutBuffer = ''; // Clear buffer
    terminalWrapper.classList.add('hidden');
    
    // Reset password toggle checkbox
    showPasswordCb.checked = false;
    passwordInput.type = 'password';
    showPasswordCb.nextElementSibling.textContent = 'Show';

    // Reset statuses
    updateTopStatus('disconnected', 'Disconnected');
    updateLowerStatus('disconnected', 'disconnected');

    if (term) {
      term.dispose();
      term = null;
    }
  }
});
