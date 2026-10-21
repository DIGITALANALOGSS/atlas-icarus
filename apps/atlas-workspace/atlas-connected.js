
(() => {
  const el = id => document.getElementById(id);

  // atlas-composer-gradient-v1: presentation only; no message-flow changes.
  const composerStyle = document.createElement("style");
  composerStyle.id = "atlas-composer-gradient-v1";
  composerStyle.textContent = `
    #notes.atlas-composer-highlight {
      box-sizing: border-box;
      width: 100%;
      max-width: 100%;
      min-height: 10rem;
      padding: 1rem 1.1rem;
      border: 3px solid transparent;
      border-radius: 20px;
      background:
        linear-gradient(#101827, #101827) padding-box,
        linear-gradient(
          115deg,
          #67e8f9 0%,
          #38bdf8 34%,
          #a78bfa 70%,
          #e9a8ff 100%
        ) border-box;
      color: #f8fafc;
      caret-color: #f8fafc;
      font-size: 1.125rem;
      line-height: 1.6;
      resize: vertical;
      box-shadow:
        0 0 0 1px rgba(103, 232, 249, 0.22),
        0 0 18px rgba(56, 189, 248, 0.14),
        0 0 30px rgba(167, 139, 250, 0.10);
      scroll-margin-block: 2rem;
    }

    #notes.atlas-composer-highlight::placeholder {
      color: #cbd5e1;
      opacity: 1;
    }

    #notes.atlas-composer-highlight:focus {
      outline: 3px solid #f8fafc;
      outline-offset: 4px;
      box-shadow:
        0 0 0 1px rgba(103, 232, 249, 0.40),
        0 0 22px rgba(56, 189, 248, 0.22),
        0 0 34px rgba(167, 139, 250, 0.16);
    }

    @media (forced-colors: active) {
      #notes.atlas-composer-highlight {
        background: Canvas;
        color: CanvasText;
        caret-color: CanvasText;
        border: 3px solid CanvasText;
        box-shadow: none;
        forced-color-adjust: auto;
      }

      #notes.atlas-composer-highlight:focus {
        outline: 3px solid Highlight;
        outline-offset: 4px;
        box-shadow: none;
      }
    }
  `;
  document.head.append(composerStyle);

  const panel = document.createElement("section");
  panel.className = "card";
  panel.setAttribute("aria-label", "Connected Atlas conversation");
  panel.innerHTML = `
    <h3>Atlas conversation</h3>
    <p id="connectionState" role="status">Connecting to Atlas…</p>
    <label for="savedConversations">Saved conversations</label>
    <select id="savedConversations"><option value="">Choose a conversation</option></select>
    <div class="actions">
      <button id="createConversation">New conversation</button>
      <button id="refreshHistory">Refresh history</button>
    </div>
    <label for="assistantProject">Conversation project context</label>
    <select id="assistantProject" disabled>
      <option value="">Personal memory only</option>
    </select>
    <p id="assistantContext" role="status" aria-live="polite">
      Loading context settings…
    </p>
    <p class="muted">Choose the project before sending the first turn.
    The association then stays fixed. Deleting saved memory does not
    erase earlier messages or information already sent to the model.</p>
    <div id="chatHistory" role="region" aria-label="Conversation messages"
         tabindex="0" style="max-height:460px;overflow:auto;margin-top:18px"></div>
    <p>Use Working notes below as your message, or use Dictate notes.
    Sending submits that text to your configured Atlas model.</p>
    <div class="actions">
      <button id="sendAtlas">Send message to Atlas</button>
      <button id="retryAtlas" hidden>Resume pending message</button>
      <button id="readReply">Read latest reply</button>
      <label><input id="speakReplies" type="checkbox" style="width:auto">
      Speak new replies automatically</label>
    </div>
    <p class="muted">Local development connection only.
    Other workspaces retain their existing integration status.</p>`;
  el("main").insertBefore(panel, el("main").children[2]);

  let cid = "", busy = false, pending = null, latest = "";
  const pendingKey = "atlas-connected-pending-v1";

  function status(text) {
    el("connectionState").textContent = text;
    el("status").textContent = text;
  }

  function show() {
    panel.hidden = el("title").textContent !== "Assistant";
    const composer = el("notes");
    if (composer) {
      composer.classList.toggle("atlas-composer-highlight", !panel.hidden);
    }
  }
  new MutationObserver(show).observe(el("title"), {
    childList:true, characterData:true, subtree:true
  });
  show();

  function lock(value) {
    busy = value;
    ["sendAtlas","createConversation","refreshHistory","savedConversations","assistantProject"]
      .forEach(id => el(id).disabled = value);
    el("retryAtlas").disabled = value;
  }

  async function api(path, body) {
    const response = await fetch("/api" + path, {
      method: body === undefined ? "GET" : "POST",
      headers: {
        "Content-Type":"application/json",
        "X-Atlas-Workspace":"1"
      },
      ...(body === undefined ? {} : {body:JSON.stringify(body)})
    });
    let data;
    try { data = await response.json(); }
    catch { throw Error("Bridge returned an unreadable response."); }
    if (!response.ok) {
      const detail = typeof data.detail === "string"
        ? data.detail : "Request rejected";
      throw Error("HTTP " + response.status + ": " + detail);
    }
    return data;
  }

  function rememberPending() {
    try {
      if (pending) sessionStorage.setItem(pendingKey, JSON.stringify(pending));
      else sessionStorage.removeItem(pendingKey);
    } catch {}
    el("retryAtlas").hidden = !pending;
  }

  function speak(text) {
    if (!("speechSynthesis" in window)) {
      status("Browser speech playback is unavailable."); return;
    }
    speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.onerror = () => status("Speech playback failed; reply remains in history.");
    speechSynthesis.speak(utterance);
  }

  async function history() {
    if (!cid) return;
    el("chatHistory").replaceChildren();
    latest = "";
    let offset = 0;
    for (let page = 0; page < 50; page++) {
      const data = await api(`/conversations/${cid}/messages?limit=200&offset=${offset}`);
      if (!Array.isArray(data.items)) throw Error("Unexpected history response.");
      for (const message of data.items) {
        const article = document.createElement("article");
        article.style.cssText = "border-bottom:1px solid #43516b;padding:12px 0";
        const heading = document.createElement("h4");
        heading.textContent = message.role === "assistant" ? "Atlas" : message.role;
        const content = document.createElement("p");
        content.style.whiteSpace = "pre-wrap";
        content.textContent = message.content;
        article.append(heading, content);
        el("chatHistory").append(article);
        if (message.role === "assistant") latest = message.content;
      }
      offset += data.items.length;
      if (data.items.length < 200) return;
    }
    throw Error("Showing the first 10,000 messages. History pagination needs expansion.");
  }

  let contextBound = false;
  let contextReady = false;
  let newProject = null;

  async function loadProjectContext() {
    contextReady = false;
    contextBound = false;
    const select = el("assistantProject");
    select.disabled = true;
    el("assistantContext").textContent = "Loading context settings…";
    const projects = await api("/workspace/projects");
    if (!Array.isArray(projects.items)) {
      throw Error("Project list unavailable; sending is blocked.");
    }
    select.replaceChildren(new Option("Personal memory only", ""));
    projects.items.forEach(p => select.add(new Option(p.name, p.id)));

    let project = null, snapshot = null;
    if (cid) {
      const state = await api(
        "/workspace/context?conversation_id=" + encodeURIComponent(cid)
      );
      contextBound = state.bound === true;
      project = state.project_id || null;
      snapshot = state.snapshot;
    }
    if (pending && pending.cid === cid) {
      if (!Object.prototype.hasOwnProperty.call(pending, "projectId")) {
        pending.projectId = project;
        rememberPending();
      }
      if (contextBound && (pending.projectId || null) !== project) {
        throw Error("Pending message conflicts with the saved project.");
      }
      project = pending.projectId || null;
    } else if (!contextBound && newProject !== null) {
      project = newProject || null;
    }
    newProject = null;
    if (project && !projects.items.some(p => p.id === project)) {
      throw Error("Saved project unavailable; sending is blocked.");
    }
    select.value = project || "";
    let summary = contextBound
      ? "Saved project association. Start a new conversation to change projects."
      : "Choose a project, or use personal memory only.";
    if (snapshot) {
      summary += " Latest prepared context: " + snapshot.selected_count +
        " selected; " + snapshot.shortened_count + " shortened; " +
        snapshot.omitted_count + " omitted. Not proof of model execution.";
    } else {
      summary += " No context snapshot recorded.";
    }
    el("assistantContext").textContent = summary;
    contextReady = true;
  }

  async function conversations() {
    const data = await api("/conversations?limit=100&offset=0");
    if (!Array.isArray(data.items)) throw Error("Unexpected conversation response.");
    const select = el("savedConversations");
    select.replaceChildren(new Option("Choose a conversation", ""));
    for (const item of data.items) {
      select.add(new Option(item.title || "Untitled conversation", item.conversation_id));
    }
    if (cid && ![...select.options].some(option => option.value === cid)) {
      select.add(new Option("Current conversation", cid));
    }
    select.value = cid;
    await loadProjectContext();
  }

  async function create() {
    const item = await api("/conversations", {
      title: "Atlas workspace · " + new Date().toLocaleString()
    });
    if (!item.conversation_id) throw Error("Conversation ID missing.");
    cid = item.conversation_id;
    await conversations();
    await history();
  }

  async function run(work) {
    if (busy) return;
    lock(true);
    try { await work(); }
    catch (error) { status(error.message); }
    finally {
      lock(false);
      rememberPending();
      el("assistantProject").disabled =
        !contextReady || contextBound || Boolean(pending);
      el("sendAtlas").disabled = !contextReady;
      el("retryAtlas").disabled = !contextReady;
      el("savedConversations").disabled = Boolean(pending);
    }
  }

  el("createConversation").onclick = () => run(async () => {
    if (pending) throw Error("Resume the pending message before starting another conversation.");
    status("Creating conversation…");
    await create();
    status("Conversation created and saved in Atlas.");
  });

  el("savedConversations").onchange = () => run(async () => {
    cid = el("savedConversations").value;
    await loadProjectContext();
    if (!cid) {
      el("chatHistory").replaceChildren(); latest = "";
      status("Choose a conversation or start a new one."); return;
    }
    await history();
    status("Saved conversation loaded.");
  });

  el("refreshHistory").onclick = () => run(async () => {
    await conversations();
    await history();
    status("History refreshed.");
  });


  // atlas-browser-voice-stream-v1
  async function streamVoiceReply(conversationId, body) {
    const controller = new AbortController();
    const section = document.createElement("section");
    section.setAttribute("aria-label", "Streaming Atlas reply");
    const label = document.createElement("p");
    label.textContent = "Connecting. Streamed text and speech are provisional until saved.";
    const preview = document.createElement("p");
    preview.style.whiteSpace = "pre-wrap";
    const stop = document.createElement("button");
    stop.textContent = "Stop reply";
    stop.disabled = true;
    section.append(label, preview, stop);
    panel.append(section);

    let turnId = null;
    let reader = null;
    let result = null;
    let text = "";
    let speechBuffer = "";
    let stopRequested = false;
    let cancelConfirmed = false;
    let cancellationRunning = false;
    const utterances = new Set();
    const wantedSpeech = el("speakReplies").checked;

    if ("speechSynthesis" in window) speechSynthesis.cancel();

    function queueSpeech(value) {
      if (!wantedSpeech || stopRequested || !value.trim()
          || !("speechSynthesis" in window)) return;
      const utterance = new SpeechSynthesisUtterance(value);
      utterance.lang = document.documentElement.lang || "en-US";
      utterances.add(utterance);
      utterance.onend = () => utterances.delete(utterance);
      utterance.onerror = () => utterances.delete(utterance);
      speechSynthesis.speak(utterance);
    }

    function flushSpeech(final = false) {
      while (speechBuffer) {
        const sentence = speechBuffer.match(
          /^([\s\S]*?[.!?](?:["')\]]*)\s+)/
        );
        let length = sentence ? sentence[1].length : 0;
        if (!length && speechBuffer.length > 300) {
          length = speechBuffer.lastIndexOf(" ", 240);
          if (length <= 0) length = 240;
        }
        if (!length && final) length = speechBuffer.length;
        if (!length) return;
        queueSpeech(speechBuffer.slice(0, length));
        speechBuffer = speechBuffer.slice(length);
      }
    }

    stop.onclick = async () => {
      if (!turnId || cancellationRunning || result) return;
      cancellationRunning = true;
      stopRequested = true;
      stop.disabled = true;
      if ("speechSynthesis" in window) speechSynthesis.cancel();
      label.textContent = "Playback stopped. Requesting backend cancellation...";
      try {
        await api(
          `/conversations/${conversationId}/voice-turns/${turnId}/cancel`,
          {}
        );
        cancelConfirmed = true;
        controller.abort();
        label.textContent =
          "Backend cancellation confirmed. Partial reply is not a completed answer.";
      } catch (error) {
        label.textContent =
          "Playback stopped, but backend cancellation was not confirmed: "
          + error.message + " Refresh history before resending.";
        stop.disabled = false;
      } finally {
        cancellationRunning = false;
      }
    };

    function receive(line) {
      if (!line.trim()) return;
      const item = JSON.parse(line);
      if (!item || typeof item !== "object") {
        throw Error("Invalid voice stream event.");
      }
      if (item.type === "started") {
        if (turnId || typeof item.turn_id !== "string"
            || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(item.turn_id)) {
          throw Error("Invalid voice turn identifier.");
        }
        turnId = item.turn_id;
        stop.disabled = false;
        label.textContent =
          "Atlas is replying. Text and speech are provisional until saved.";
      } else if (item.type === "delta") {
        if (!turnId || item.turn_id !== turnId
            || typeof item.text !== "string") {
          throw Error("Invalid voice text event.");
        }
        text += item.text;
        if (text.length > 200000) throw Error("Voice reply limit exceeded.");
        if (!stopRequested) {
          preview.textContent = text;
          speechBuffer += item.text;
          flushSpeech();
        }
      } else if (item.type === "completed") {
        if (!item.result || !item.result.message
            || typeof item.result.message.content !== "string") {
          throw Error("Completed voice reply is missing.");
        }
        result = item.result;
      } else if (item.type === "error") {
        throw Error(
          typeof item.detail === "string"
            ? item.detail : "Backend voice stream failed."
        );
      } else {
        throw Error("Unknown voice stream event.");
      }
    }

    try {
      const response = await fetch(
        `/api/conversations/${conversationId}/voice-turns`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Atlas-Workspace": "1"
          },
          body: JSON.stringify(body),
          signal: controller.signal
        }
      );
      if (!response.ok) {
        throw Error(
          `Voice endpoint returned HTTP ${response.status}. `
          + "Pending message retained; refresh history before resuming."
        );
      }
      if (!(response.headers.get("Content-Type") || "")
          .toLowerCase().startsWith("application/x-ndjson")
          || !response.body) {
        throw Error("Voice endpoint did not return an event stream.");
      }

      reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8", {fatal:true});
      let buffer = "";
      let bytes = 0;
      while (!result) {
        const chunk = await reader.read();
        if (chunk.done) {
          buffer += decoder.decode();
          if (buffer.trim()) receive(buffer);
          break;
        }
        bytes += chunk.value.byteLength;
        if (bytes > 8000000) throw Error("Voice event stream limit exceeded.");
        buffer += decoder.decode(chunk.value, {stream:true});
        let newline;
        while (!result && (newline = buffer.indexOf("\n")) !== -1) {
          const line = buffer.slice(0, newline);
          buffer = buffer.slice(newline + 1);
          receive(line);
        }
      }

      if (cancelConfirmed) throw Error("Voice turn cancelled.");
      if (!result) {
        throw Error(
          "Stream ended without a saved reply. Refresh history before resuming."
        );
      }
      if (result.message.content !== text) {
        // Completed replay, or final content normalized by the backend.
        speechBuffer = "";
        if ("speechSynthesis" in window) speechSynthesis.cancel();
        queueSpeech(result.message.content);
      } else {
        flushSpeech(true);
      }
      preview.textContent = result.message.content;
      label.textContent = stopRequested
        ? "Reply completed and saved. Playback was stopped."
        : "Reply completed and saved.";
      return result;
    } catch (error) {
      if ("speechSynthesis" in window) speechSynthesis.cancel();
      if (cancelConfirmed) {
        if (pending && pending.cid === conversationId
            && pending.messageId === body.user_message_id) {
          pending = null;
          rememberPending();
        }
        label.textContent =
          "Cancellation confirmed. Your draft remains; partial reply is not completed.";
        throw Error(
          "Voice turn cancelled. Draft retained. Refresh history before sending again."
        );
      }
      label.textContent =
        "Reply not confirmed complete. Partial text is provisional. "
        + error.message;
      throw error;
    } finally {
      stop.disabled = true;
      if (reader) {
        try { await reader.cancel(); } catch {}
        reader.releaseLock();
      }
      controller.abort();
    }
  }

  async function submit(resume, voiceStream = false) {
    await run(async () => {
      if (!contextReady) throw Error("Refresh history to load context settings first.");
      if (!resume) {
        if (pending) throw Error("Resume the pending message first to avoid duplicate submissions.");
        const text = el("notes").value.trim();
        if (!text) throw Error("Add a message or dictate into Working notes first.");
        if (text.length > 20000) throw Error("Message exceeds 20,000 characters.");
        if (!cid) {
          newProject = el("assistantProject").value;
          status("Creating your conversation…");
          await create();
        }
        pending = {cid, content:text, clientId:crypto.randomUUID(), messageId:null,
          projectId:el("assistantProject").value || null};
        pending.voiceStream = voiceStream;
        rememberPending();
      }
      if (!pending) return;
      cid = pending.cid;
      status("Saving your message…");
      if (!pending.messageId) {
        const saved = await api(`/conversations/${cid}/messages`, {
          content:pending.content, client_message_id:pending.clientId
        });
        if (!saved.message_id) throw Error("Saved message ID missing.");
        pending.messageId = saved.message_id;
        rememberPending();
      }
      const usingVoiceStream = pending.voiceStream === true;
      status(usingVoiceStream
        ? "Atlas is streaming a provisional reply."
        : "Atlas is generating a reply. A disconnected request is not automatically cancelled.");
      const turnBody = {
        user_message_id:pending.messageId,
        workspace_project_id:pending.projectId || null
      };
      const result = usingVoiceStream
        ? await streamVoiceReply(cid, turnBody)
        : await api(`/conversations/${cid}/turns`, turnBody);
      if (!result.message || typeof result.message.content !== "string") {
        throw Error("Reply unavailable. Pending message retained.");
      }
      const answer = result.message.content;
      const original = pending.content;
      pending = null; rememberPending();
      if (el("notes").value.trim() === original) {
        el("notes").value = "";
        if (typeof persist === "function") persist();
      }
      await conversations();
      await history();
      status("Atlas replied. Conversation saved.");
      if (!usingVoiceStream && el("speakReplies").checked) speak(answer);
    });
  }

  el("sendAtlas").onclick = () => submit(false);
  el("retryAtlas").onclick = () => submit(true);
  el("readReply").onclick = () => {
    if (latest) speak(latest);
    else status("No assistant reply loaded.");
  };

  run(async () => {
    try {
      const stored = JSON.parse(sessionStorage.getItem(pendingKey) || "null");
      if (stored && typeof stored.cid === "string"
          && typeof stored.content === "string"
          && typeof stored.clientId === "string") {
        pending = stored; cid = stored.cid;
      }
    } catch {}
    await conversations();
    if (cid) await history();
    status(pending
      ? "Connected. A pending message can be resumed; it has not been resent."
      : "Connected to Atlas. Choose a conversation or send your first message.");
  });

  // atlas-speak-and-send-v1
  const voiceBox = document.createElement("section");
  voiceBox.setAttribute("aria-label", "Speak directly to Atlas");
  voiceBox.innerHTML = `
    <h3>Speak to Atlas</h3>
    <p>Speak one message. When recognition finishes, it is sent automatically.
    The microphone does not restart automatically after the reply.</p>
    <div class="actions">
      <button id="atlasVoiceStart">Speak and send</button>
      <button id="atlasVoiceFinish" disabled>Finish speaking and send</button>
      <button id="atlasVoiceCancel" disabled>Cancel recording</button>
    </div>
    <p id="atlasVoiceState" role="status" aria-live="polite">
    Ready. Existing notes must be empty before starting.</p>
    <p id="atlasVoicePreview" style="white-space:pre-wrap"></p>`;
  panel.append(voiceBox);

  const voiceStart = el("atlasVoiceStart");
  const voiceFinish = el("atlasVoiceFinish");
  const voiceCancel = el("atlasVoiceCancel");
  const voiceState = text => el("atlasVoiceState").textContent = text;
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  let voiceSession = null;
  let voiceConsent = false;

  if (!Recognition) {
    voiceStart.disabled = true;
    voiceState("This browser does not expose speech recognition. Existing typing and dictation controls remain available.");
  }

  voiceStart.onclick = () => {
    if (voiceSession) return;
    if (busy || pending || !contextReady) {
      voiceState("Finish or resolve the current message, and load context settings before speaking.");
      return;
    }
    if (el("notes").value.trim()) {
      voiceState("Working notes already contain text. Send or clear it first; voice will not overwrite it.");
      return;
    }
    if (!voiceConsent) {
      if (!confirm(
        "Browser speech recognition may send microphone audio to the browser provider. " +
        "Recognized speech will be sent to your configured Atlas model automatically. " +
        "The reply may be spoken while still provisional; Stop reply stops playback " +
        "and requests backend cancellation. Continue?"
      )) return;
      voiceConsent = true;
    }

    const conversationAtStart = cid;
    const projectAtStart = el("assistantProject").value;
    const originalNotes = el("notes").value;
    const recognition = new Recognition();
    let cancelled = false;
    let failure = "";
    let finalText = "";
    let timer = null;
    let finished = false;
    let savedControls = [];
    voiceSession = recognition;

    recognition.lang = document.documentElement.lang || "en-US";
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;

    function restoreControls() {
      savedControls.forEach(([control, disabled]) => {
        if (control.isConnected) control.disabled = disabled;
      });
      voiceStart.disabled = false;
      voiceFinish.disabled = true;
      voiceCancel.disabled = true;
    }

    function finishSession() {
      if (finished) return false;
      finished = true;
      clearTimeout(timer);
      if (voiceSession === recognition) voiceSession = null;
      restoreControls();
      return true;
    }

    recognition.onresult = event => {
      const finals = [], preview = [];
      for (let i = 0; i < event.results.length; i++) {
        const result = event.results[i];
        preview.push(result[0].transcript);
        if (result.isFinal) finals.push(result[0].transcript);
      }
      finalText = finals.join(" ").trim();
      el("atlasVoicePreview").textContent = preview.join(" ").trim();
    };

    recognition.onerror = event => {
      failure = event.error || "recognition failed";
      voiceState("Recognition stopped: " + failure + ". Nothing will be sent.");
    };

    recognition.onend = async () => {
      if (!finishSession()) return;
      if (cancelled || failure) {
        voiceState(cancelled
          ? "Recording cancelled. Nothing sent."
          : "Recognition failed: " + failure + ". Nothing sent.");
        return;
      }
      if (!finalText) {
        voiceState("No final speech was recognized. Nothing sent.");
        return;
      }
      if (busy || pending || !contextReady ||
          cid !== conversationAtStart ||
          el("assistantProject").value !== projectAtStart ||
          el("notes").value !== originalNotes) {
        voiceState("Conversation or draft changed. Nothing sent; recognized text remains shown below.");
        return;
      }
      if (finalText.length > 20000) {
        voiceState("Recognized message is too long. Nothing sent.");
        return;
      }
      el("notes").value = finalText;
      el("notes").dispatchEvent(new Event("input", {bubbles:true}));
      if (typeof persist === "function") persist();
      el("speakReplies").checked = true;
      voiceStart.disabled = true;
      voiceState("Message recognized. Sending to Atlas...");
      try {
        await submit(false, true);
        voiceState(pending
          ? "Message is pending. Use the existing status and Resume controls; it was not automatically resent."
          : (el("notes").value.trim() === finalText
              ? "Submission did not complete. Check the conversation status; your text remains in Working notes."
              : "Voice submission finished. The microphone is off; the reply uses automatic speech playback."));
      } catch (error) {
        voiceState("Voice submission stopped: " + error.message);
      } finally {
        voiceStart.disabled = false;
      }
    };

    voiceFinish.onclick = () => {
      if (voiceSession !== recognition) return;
      voiceFinish.disabled = true;
      voiceState("Finishing recognition...");
      try { recognition.stop(); }
      catch {
        cancelled = true;
        recognition.abort();
        finishSession();
        voiceState("Recognition could not finish. Nothing sent.");
      }
    };

    voiceCancel.onclick = () => {
      if (voiceSession !== recognition) return;
      cancelled = true;
      recognition.abort();
      finishSession();
      voiceState("Recording cancelled. Nothing sent.");
    };

    savedControls = [...document.querySelectorAll(
      "button,select,textarea,input"
    )].filter(control =>
      control !== voiceFinish && control !== voiceCancel
    ).map(control => [control, control.disabled]);
    savedControls.forEach(([control]) => control.disabled = true);
    voiceFinish.disabled = false;
    voiceCancel.disabled = false;

    try {
      if ("speechSynthesis" in window) speechSynthesis.cancel();
      el("atlasVoicePreview").textContent = "";
      voiceState("Listening. Speak now; recognition will send the final message automatically.");
      recognition.start();
      timer = setTimeout(() => {
        cancelled = true;
        recognition.abort();
        finishSession();
        voiceState("Recording timed out. Nothing sent.");
      }, 90000);
    } catch (error) {
      cancelled = true;
      finishSession();
      voiceState("Microphone could not start: " + error.message);
    }
  };

})();
