(() => {
  'use strict';

  const MAX_MESSAGE_LENGTH = 4000;
  const MAX_HISTORY_MESSAGES = 20;
  const MAX_HISTORY_MESSAGE_LENGTH = 12000;
  const MAX_HISTORY_LENGTH = 24000;
  // Single source of truth for which Foundry agent each panel calls, its
  // underlying kind (server-orchestrated prompt vs. self-hosted container),
  // and its baked-in reasoning effort. Panel ordering here drives the 2x2
  // results grid: row 1 is the low-reasoning pair, row 2 is no-reasoning.
  const AGENT_META = {
    prompt: { kind: 'prompt', reasoningLabel: 'Prompt agent', mark: '01' },
    hosted: { kind: 'hosted', reasoningLabel: 'Hosted agent', mark: '02' },
    prompt_none: { kind: 'prompt', reasoningLabel: 'Prompt agent', mark: '03' },
    hosted_none: { kind: 'hosted', reasoningLabel: 'Hosted agent', mark: '04' }
  };
  const agents = Object.keys(AGENT_META);
  const isHosted = (agent) => AGENT_META[agent].kind === 'hosted';
  const form = document.getElementById('compare-form');
  const messageInput = document.getElementById('message');
  const submitButton = document.getElementById('submit');
  const resetButton = document.getElementById('reset');
  const status = document.getElementById('status');
  const formError = document.getElementById('form-error');
  let history = Object.fromEntries(agents.map((agent) => [agent, []]));
  let continuation = Object.fromEntries(agents.map((agent) => [agent, null]));
  let pending = false;

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function safeText(value) {
    if (typeof value === 'string') return value;
    if (value === null || value === undefined) return '';
    return JSON.stringify(value, null, 2);
  }

  function updateCount() {
    document.getElementById('character-count').textContent =
      `${messageInput.value.length.toLocaleString()} / 4,000`;
  }

  function setPending(value) {
    pending = value;
    submitButton.disabled = value;
    resetButton.disabled = value;
    messageInput.readOnly = value;
    submitButton.textContent = value ? 'Comparing…' : 'Compare answers ↗';
    for (const agent of agents) {
      document.getElementById(`${agent}-panel`).setAttribute('aria-busy', String(value));
    }
  }

  function setPanelState(agent, label, state = '') {
    const badge = document.getElementById(`${agent}-state`);
    badge.textContent = label;
    badge.className = `state ${state}`.trim();
  }

  function renderCorrelation(agent, result = {}) {
    const section = element('details', undefined, 'diagnostics');
    const heading = element('summary', 'Conversation & telemetry');
    heading.id = `${agent}-correlation-heading`;
    section.setAttribute('aria-labelledby', heading.id);
    const body = element('div', undefined, 'diagnostics-body');
    const values = element('dl');
    const feedback = element('p', '', 'copy-status');
    feedback.setAttribute('role', 'status');
    feedback.setAttribute('aria-live', 'polite');
    feedback.setAttribute('aria-atomic', 'true');
    const identifiers = [
      [result.conversation_scope === 'hosted_model' ? 'Hosted model conversation' : 'Foundry conversation', result.conversation_id],
      ['Foundry agent response', result.response_id],
      ['UI invocation · App Insights operation / trace ID', result.trace_id],
      ['UI invocation span ID', result.span_id]
    ];
    if (isHosted(agent)) {
      identifiers.splice(2, 0, ['Hosted model response', result.model_response_id]);
      identifiers.push(
        ['Hosted runtime · App Insights operation / trace ID', result.hosted_runtime_trace_id],
        ['Hosted runtime span ID', result.hosted_runtime_span_id]
      );
    }
    for (const [label, value] of identifiers) {
      const reported = typeof value === 'string' && value.trim().length > 0;
      const item = element('div');
      const description = element('dd');
      description.append(element('span', reported ? value : 'Not reported', 'correlation-value'));
      if (reported) {
        const copy = element('button', 'Copy', 'button secondary copy-id');
        copy.type = 'button';
        copy.setAttribute('aria-label', `Copy ${label} for ${AGENT_META[agent].reasoningLabel}`);
        copy.addEventListener('click', async () => {
          copy.disabled = true;
          feedback.textContent = `Copying ${label}…`;
          try {
            if (typeof navigator === 'undefined' || !navigator.clipboard?.writeText) {
              throw new Error('Clipboard unavailable');
            }
            await navigator.clipboard.writeText(value);
            feedback.textContent = `${label} copied.`;
          } catch {
            feedback.textContent = `Could not copy ${label}. Select and copy the value manually.`;
          } finally {
            copy.disabled = false;
          }
        });
        description.append(copy);
      }
      item.append(element('dt', label), description);
      values.append(item);
    }
    section.append(heading, body);
    body.append(values);
    const conversationNote = safeText(result.conversation_note);
    if (conversationNote.trim()) {
      body.append(element('p', conversationNote, 'evidence-note'));
    }
    const telemetryNote = safeText(result.telemetry_note);
    if (telemetryNote.trim()) {
      body.append(element('p', telemetryNote, 'evidence-note'));
    }
    const hostedTelemetryNote = safeText(result.hosted_runtime_telemetry_note);
    if (isHosted(agent) && hostedTelemetryNote.trim()) {
      body.append(element('p', `Hosted runtime telemetry: ${hostedTelemetryNote}`, 'evidence-note'));
    }
    body.append(feedback);
    return section;
  }

  function showPanelError(agent, error, result = {}) {
    const content = document.getElementById(`${agent}-content`);
    const notice = element('div', undefined, 'error-notice');
    notice.append(
      element('h4', 'This agent could not complete the request'),
      element('p', safeText(error)),
      element('p', 'This turn was not added to this agent’s history. Follow the error guidance above, or select New comparison to clear both histories.')
    );
    content.replaceChildren(notice, renderCorrelation(agent, result));
    setPanelState(agent, 'Error', 'error');
    return content;
  }

  function metric(label, value) {
    const item = element('div', undefined, 'metric');
    item.append(element('dt', label), element('dd', value));
    return item;
  }

  function tokenCount(value) {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0
      ? value.toLocaleString()
      : 'Not reported';
  }

  function renderMetrics(result) {
    const metrics = element('dl', undefined, 'metrics');
    const latency = result.latency_ms;
    const latencyText = typeof latency === 'number' && Number.isFinite(latency) && latency >= 0
      ? `${(latency / 1000).toLocaleString(undefined, { maximumFractionDigits: 2 })} s`
      : 'Not reported';
    metrics.append(
      metric('Latency', latencyText),
      metric('Input tokens', tokenCount(result.usage?.input_tokens)),
      metric('Output tokens', tokenCount(result.usage?.output_tokens)),
      metric('Reasoning tokens', tokenCount(result.usage?.output_tokens_details?.reasoning_tokens))
    );
    const note = element('p',
      'Each turn\u2019s counts come straight from that call\u2019s own response and are not carried over from '
      + 'earlier turns. Input tokens still are not directly comparable across agents: the prompt agent\u2019s '
      + 'Foundry-managed conversation resends the full prior history \u2014 including earlier tool results \u2014 '
      + 'on every turn, while the hosted agent replays only the plain text history and re-searches fresh each time.',
      'evidence-note'
    );
    return [metrics, note];
  }

  function renderTools(result) {
    const section = element('section', undefined, 'evidence-section');
    section.append(element('h4', 'Native tool evidence'));
    const calls = Array.isArray(result.tool_calls) ? result.tool_calls : [];
    const available = result.tool_evidence_available === true && Array.isArray(result.tool_calls);
    const truncated = result.tool_evidence_truncated === true;
    section.append(element('p',
      available
        ? `${truncated ? 'At least ' : ''}${calls.length} observed tool record${calls.length === 1 ? '' : 's'}`
        : 'Not exposed by response',
      'evidence-status'
    ));
    if (truncated) {
      section.append(element('p', 'Tool evidence was truncated; additional records or details may be omitted.', 'evidence-note'));
    }
    const note = safeText(result.tool_evidence_note);
    if (note.trim()) section.append(element('p', note, 'evidence-note'));
    for (const [index, call] of calls.entries()) {
      const details = element('details');
      const name = call && typeof call === 'object'
        ? call.name || call.function?.name || call.tool_name || call.type
        : null;
      details.append(
        element('summary', `Tool record ${index + 1}${name ? ` · ${safeText(name)}` : ''}`),
        element('pre', safeText(call), 'tool-json')
      );
      section.append(details);
    }
    return section;
  }

  function renderResult(agent, result, question) {
    if (!result || typeof result !== 'object' || Array.isArray(result)) {
      showPanelError(agent, 'The server did not return a valid result for this agent.');
      return false;
    }
    const hasError = result.error !== null && result.error !== undefined && result.error !== '';
    const hasText = typeof result.text === 'string' && result.text.trim().length > 0;
    let content;
    if (hasError || !hasText) {
      content = showPanelError(agent, hasError ? result.error : 'No answer text was returned.', result);
    } else {
      content = document.getElementById(`${agent}-content`);
      content.replaceChildren();
      setPanelState(agent, 'Complete', 'success');
    }
    if (hasText) {
      const answer = element('section', undefined, 'answer-section');
      answer.append(
        element('h4', hasError || result.output_truncated === true ? 'Partial answer' : 'Answer'),
        element('div', result.text, 'answer-text')
      );
      content.append(answer);
    }
    if (result.output_truncated === true) {
      content.append(element('p',
        'The returned answer was truncated. Some answer text may be omitted.',
        'evidence-note'
      ));
    }
    content.append(...renderMetrics(result), renderTools(result));
    if (!hasError && hasText) {
      history[agent] = [
        ...history[agent],
        { role: 'user', content: question },
        { role: 'assistant', content: result.text.slice(0, MAX_HISTORY_MESSAGE_LENGTH) }
      ].slice(-MAX_HISTORY_MESSAGES);
      while (history[agent].reduce((length, message) => length + message.content.length, 0) > MAX_HISTORY_LENGTH) {
        history[agent].splice(0, 2);
      }
      if (result.text.length > MAX_HISTORY_MESSAGE_LENGTH) {
        content.append(element('p',
          'The returned text is displayed in full. Only its first 12,000 characters are kept in text history.',
          'evidence-note'
        ));
      }
      content.append(renderCorrelation(agent, result));
      continuation[agent] = typeof result.continuation === 'string' ? result.continuation : null;
      return true;
    }
    return false;
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (pending) return;
    const question = messageInput.value.trim();
    formError.hidden = true;
    if (!question || messageInput.value.length > MAX_MESSAGE_LENGTH) {
      formError.textContent = 'Enter a question between 1 and 4,000 characters.';
      formError.hidden = false;
      messageInput.focus();
      return;
    }

    setPending(true);
    document.getElementById('comparison-id').textContent = 'Comparison ID: pending';
    document.getElementById('last-question').hidden = false;
    document.getElementById('last-question-text').textContent = question;
    status.textContent = 'Comparing all four agents. Answers and tool evidence will appear when the server responds.';
    for (const agent of agents) {
      setPanelState(agent, 'Working', 'working');
      document.getElementById(`${agent}-content`).replaceChildren(
        element('p', 'Waiting for the agent’s answer and available search evidence…', 'empty-state')
      );
    }

    try {
      const response = await fetch('/api/compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ message: question, history, continuation })
      });
      if (!response.ok) {
        throw new Error(`The comparison request failed (HTTP ${response.status}). Please try again.`);
      }
      const data = await response.json();
      if (!data || typeof data !== 'object' || Array.isArray(data)) {
        throw new Error('The server returned an invalid comparison response. Please try again.');
      }
      document.getElementById('comparison-id').textContent =
        `Comparison ID: ${safeText(data.comparison_id) || 'Not reported'}`;
      const outcomes = agents.map((agent) => renderResult(agent, data[agent], question));
      const completed = outcomes.filter(Boolean).length;
      status.textContent = completed === agents.length
        ? 'All answers are ready. Compare the responses and expand the tool evidence below.'
        : completed === 0
          ? 'No agent completed the request. All conversation histories are unchanged.'
          : `${completed} of ${agents.length} answers are ready. Agents that reported an error kept their prior history.`;
      if (completed === agents.length) {
        messageInput.value = '';
        updateCount();
      }
    } catch (error) {
      const text = error instanceof TypeError
        ? 'Could not reach the comparison service. Check your connection and try again.'
        : error instanceof SyntaxError
          ? 'The server returned unreadable data. Please try again.'
          : error.message || 'The comparison failed. Please try again.';
      for (const agent of agents) showPanelError(agent, text);
      document.getElementById('comparison-id').textContent = 'Comparison ID: unavailable';
      status.textContent = 'The comparison failed. Both conversation histories are unchanged.';
    } finally {
      setPending(false);
    }
  });

  messageInput.addEventListener('input', () => {
    updateCount();
    formError.hidden = true;
  });

  resetButton.addEventListener('click', () => {
    if (pending) return;
    history = Object.fromEntries(agents.map((agent) => [agent, []]));
    continuation = Object.fromEntries(agents.map((agent) => [agent, null]));
    messageInput.value = '';
    updateCount();
    formError.hidden = true;
    document.getElementById('comparison-id').textContent = 'Comparison ID: —';
    document.getElementById('last-question').hidden = true;
    document.getElementById('last-question-text').textContent = '';
    status.textContent = 'Browser histories and continuation tokens cleared. Foundry-stored prompt conversations and responses are not deleted. Start a new comparison.';
    for (const agent of agents) {
      setPanelState(agent, 'Ready');
      const empty = element('div', undefined, 'empty-state');
      const mark = element('span', AGENT_META[agent].mark, 'empty-mark');
      mark.setAttribute('aria-hidden', 'true');
      empty.append(
        mark,
        element('p', 'Ready for your question', 'empty-title'),
        element('p', 'Compare the answer, timing, and tokens.\nInspect available search evidence below.')
      );
      document.getElementById(`${agent}-content`).replaceChildren(empty);
    }
    messageInput.focus();
  });
})();
