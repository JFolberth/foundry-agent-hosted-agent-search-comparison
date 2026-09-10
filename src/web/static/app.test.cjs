const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');

test('introduces metadata-grounded book recommendations without promising plot summaries', () => {
  assert.match(html, /One book catalog\. Four agents\./);
  assert.match(html, /Recommend three books by Agatha Christie/);
  assert.match(html, /book metadata, not plot summaries/);
});

class Node {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.listeners = {};
    this.attributes = {};
    this.value = '';
    this.hidden = false;
    this.ownText = '';
  }
  set textContent(value) {
    this.ownText = String(value);
    this.children = [];
  }
  get textContent() {
    return this.ownText + this.children.map((child) => child.textContent).join('');
  }
  set innerHTML(_) { throw new Error('Unsafe HTML rendering'); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText = ''; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  focus() { this.focused = true; }
}

function setup(fetchHandler, navigator = {}, configResponse = { ok: true, json: async () => ({ deployed_at: null }) }) {
  const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new Node()]));
  const requests = [];
  vm.runInNewContext(source, {
    document: {
      getElementById(id) {
        assert.ok(nodes.has(id), `HTML must define ${id}`);
        return nodes.get(id);
      },
      createElement: (tag) => new Node(tag)
    },
    fetch: async (url, options) => {
      if (url === '/api/config') return configResponse;
      requests.push({ url, ...options, body: JSON.parse(options.body) });
      return fetchHandler(requests.length);
    },
    URL, TypeError, SyntaxError, navigator, crypto
  }, { filename: path.join(__dirname, 'app.js') });
  return {
    nodes, requests,
    async submit(message = 'Demo question') {
      nodes.get('message').value = message;
      await nodes.get('compare-form').listeners.submit({ preventDefault() {} });
    },
    reset() { nodes.get('reset').listeners.click(); },
    content(agent) { return nodes.get(`${agent}-content`).textContent; }
  };
}

function result(overrides = {}) {
  return {
    text: 'An answer', error: null, latency_ms: 1500,
    usage: { input_tokens: 20, output_tokens: 15, output_tokens_details: { reasoning_tokens: 0 } },
    tool_calls: [], tool_evidence_available: true, citations: [],
    ...overrides
  };
}

function response(prompt = result(), hosted = result(), extra = {}) {
  const { prompt_none = result(), hosted_none = result(), ...rest } = extra;
  return { ok: true, json: async () => ({ comparison_id: 'comparison-123', prompt, hosted, prompt_none, hosted_none, ...rest }) };
}

function descendants(node) {
  return node.children.flatMap((child) => [child, ...descendants(child)]);
}

test('renders the deploy timestamp reported by the backend, or a fallback when unavailable', async () => {
  const withStamp = setup(() => response(), {}, {
    ok: true, json: async () => ({ deployed_at: '2026-01-02T03:04:05Z' }),
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(withStamp.nodes.get('deployed-at').textContent, /^Deployed: /);
  assert.doesNotMatch(withStamp.nodes.get('deployed-at').textContent, /unavailable/);

  const withoutStamp = setup(() => response(), {}, { ok: false });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(withoutStamp.nodes.get('deployed-at').textContent, 'Deployed: unavailable');
});

test('loads only same-origin external script and stylesheet without inline CSP exceptions', () => {
  const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];
  assert.equal(scripts.length, 1);
  assert.match(scripts[0][1], /\bsrc="\/static\/app\.js"/);
  assert.equal(scripts[0][2].trim(), '');
  assert.match(html, /<link rel="stylesheet" href="\/static\/styles\.css">/);
  assert.doesNotMatch(html, /<style\b|\s(?:style|on\w+)\s*=/i);
  assert.doesNotMatch(source, /\beval\s*\(|\bnew\s+Function\s*\(|\.innerHTML\s*=|\.style\s*[.=]/);
  const css = fs.readFileSync(path.join(__dirname, 'styles.css'), 'utf8');
  assert.doesNotMatch(css, /@import\b|url\s*\(/i);
});

test('discloses different agent storage semantics and distinguishes local reset or token expiry from deletion', async () => {
  const notice = html.match(/<aside\b[^>]*>([\s\S]*?)<\/aside>/)[1].replace(/\s+/g, ' ');
  assert.match(notice, /Only use non-sensitive demo content/);
  assert.match(notice, /Prompt agents:<\/strong> Foundry stores their conversations and responses/);
  assert.match(notice, /Hosted agents:<\/strong> replay their browser-held history on each turn with <code>store=False<\/code>/);
  assert.match(notice, /not a guarantee of zero service-side retention/);
  assert.match(notice, /Neither this reset nor expiry of the demo’s 20-minute continuation token deletes Foundry-stored prompt conversations or responses/);
  assert.doesNotMatch(notice, /stores conversations and responses for both agents/);
  assert.ok(notice.indexOf('Only use non-sensitive demo content') < notice.indexOf('<details'));
  assert.match(notice, /<details class="privacy-details"> <summary>How your data is handled<\/summary>/);
  const app = setup(() => response(
    result({ continuation: 'prompt-private' }), result({ continuation: 'hosted-private' })
  ));
  await app.submit('First');
  app.reset();
  assert.equal(app.requests.length, 1);
  assert.match(app.nodes.get('status').textContent, /Browser histories and continuation tokens cleared/);
  assert.match(app.nodes.get('status').textContent, /Foundry-stored prompt conversations and responses are not deleted/);
  await app.submit('New conversation');
  assert.deepEqual(app.requests[1].body.history, { prompt: [], hosted: [], prompt_none: [], hosted_none: [] });
  assert.deepEqual(app.requests[1].body.continuation, { prompt: null, hosted: null, prompt_none: null, hosted_none: null });
});

test('discloses server-truncated output separately from tool evidence and text-history clipping', async () => {
  const token = 'a'.repeat(43);
  const app = setup(() => response(
    result({ text: 'A'.repeat(13000), output_truncated: true, continuation: token }),
    result({ output_truncated: false })
  ));
  await app.submit();
  assert.match(app.content('prompt'), /Partial answer/);
  assert.match(app.content('prompt'), /The returned answer was truncated. Some answer text may be omitted/);
  assert.match(app.content('prompt'), /Only its first 12,000 characters are kept in text history/);
  assert.doesNotMatch(app.content('prompt'), /Tool evidence was truncated/);
  assert.doesNotMatch(app.content('hosted'), /Partial answer|returned answer was truncated/);
  assert.equal(app.content('prompt').includes(token), false);
  await app.submit('Next');
  assert.equal(app.requests[1].body.continuation.prompt, token);
  assert.equal(app.requests[1].body.history.prompt[1].content.length, 12000);
  const absent = setup(() => response());
  await absent.submit();
  assert.doesNotMatch(absent.content('prompt'), /returned answer was truncated/);
});

test('uses one backend request, independently retains only successful history, and resets both', async () => {
  const app = setup((count) => count === 1
    ? response(result({ text: 'Prompt answer' }), result({ text: 'Partial', error: 'Agent unavailable' }))
    : response(result({ text: 'Next prompt' }), result({ text: 'Hosted answer' })));
  await app.submit('  First question  ');
  assert.equal(app.requests[0].url, '/api/compare');
  assert.equal(app.requests[0].method, 'POST');
  assert.deepEqual(app.requests[0].body, {
    message: 'First question',
    history: { prompt: [], hosted: [], prompt_none: [], hosted_none: [] },
    continuation: { prompt: null, hosted: null, prompt_none: null, hosted_none: null },
    session: app.requests[0].body.session
  });
  assert.equal(typeof app.requests[0].body.session.hosted, 'string');
  assert.equal(typeof app.requests[0].body.session.hosted_none, 'string');
  assert.match(app.content('hosted'), /Agent unavailable/);
  assert.match(app.content('hosted'), /Partial answer/);
  await app.submit('Follow-up');
  assert.deepEqual(app.requests[1].body.history, {
    prompt: [{ role: 'user', content: 'First question' }, { role: 'assistant', content: 'Prompt answer' }],
    hosted: [],
    prompt_none: [{ role: 'user', content: 'First question' }, { role: 'assistant', content: 'An answer' }],
    hosted_none: [{ role: 'user', content: 'First question' }, { role: 'assistant', content: 'An answer' }]
  });
  assert.equal(app.nodes.get('comparison-id').textContent, 'Comparison ID: comparison-123');
  app.reset();
  assert.equal(app.nodes.get('last-question').hidden, true);
  await app.submit('Fresh question');
  assert.deepEqual(app.requests[2].body.history, { prompt: [], hosted: [], prompt_none: [], hosted_none: [] });
});

test('bounds each history to twenty messages without mixing agent answers', async () => {
  const app = setup((count) => response(
    result({ text: `Prompt ${count}` }), result({ text: `Hosted ${count}` })
  ));
  for (let i = 1; i <= 13; i++) await app.submit(`Question ${i}`);
  const history = app.requests[12].body.history;
  for (const agent of ['prompt', 'hosted']) {
    assert.equal(history[agent].length, 20);
    assert.deepEqual(history[agent][0], { role: 'user', content: 'Question 3' });
    assert.equal(history[agent][1].content, `${agent === 'prompt' ? 'Prompt' : 'Hosted'} 3`);
    assert.equal(history[agent][19].content, `${agent === 'prompt' ? 'Prompt' : 'Hosted'} 12`);
  }
});

test('enforces per-message and aggregate history character limits while retaining full displayed answers', async () => {
  const longAnswer = 'A'.repeat(13000);
  const app = setup((count) => response(
    result({ text: longAnswer, continuation: `prompt-${count}` }),
    result({ text: 'B'.repeat(7000), continuation: `hosted-${count}` })
  ));
  for (let i = 1; i <= 4; i++) await app.submit(String(i).repeat(4000));
  const latest = app.requests[3].body;
  for (const agent of ['prompt', 'hosted']) {
    const messages = latest.history[agent];
    assert.ok(messages.every((message) => message.content.length <= 12000));
    assert.ok(messages.reduce((total, message) => total + message.content.length, 0) <= 24000);
    assert.equal(messages.length % 2, 0);
    for (let i = 0; i < messages.length; i += 2) {
      assert.equal(messages[i].role, 'user');
      assert.equal(messages[i + 1].role, 'assistant');
    }
  }
  assert.equal(latest.history.prompt.length, 2);
  assert.equal(latest.history.prompt[0].content, '3'.repeat(4000));
  assert.equal(latest.history.prompt[1].content.length, 12000);
  assert.equal(latest.history.hosted.length, 4);
  assert.equal(latest.history.hosted[0].content, '2'.repeat(4000));
  assert.deepEqual(latest.continuation, { prompt: 'prompt-3', hosted: 'hosted-3', prompt_none: null, hosted_none: null });
  assert.ok(app.content('prompt').includes(longAnswer));
  assert.match(app.content('prompt'), /Only its first 12,000 characters are kept in text history/);
  assert.doesNotMatch(app.content('hosted'), /Only its first/);
});

test('expired continuation errors retain prior side state and display reset guidance without automatic fallback', async () => {
  const app = setup((count) => {
    if (count === 2) return response(
      result({ text: null, error: 'Continuation expired. Start a new comparison.', continuation: null }),
      result({ text: 'Hosted follow-up', continuation: 'hosted-next' })
    );
    return response(result({ continuation: 'prompt-original' }), result({ continuation: 'hosted-original' }));
  });
  await app.submit('First');
  await app.submit('Second');
  assert.equal(app.requests.length, 2);
  assert.match(app.content('prompt'), /Continuation expired. Start a new comparison/);
  assert.match(app.content('prompt'), /select New comparison/);
  await app.submit('Third');
  assert.deepEqual(app.requests[2].body.continuation, { prompt: 'prompt-original', hosted: 'hosted-next', prompt_none: null, hosted_none: null });
  assert.equal(app.requests[2].body.history.prompt.length, 2);
  assert.equal(app.requests[2].body.history.hosted.length, 4);
});

test('distinguishes unavailable tool evidence from zero observed records and preserves raw details', async () => {
  const raw = { function: { name: 'azure_ai_search' }, arguments: { query: '<img src=x onerror=alert(1)>' } };
  const app = setup(() => response(
    result({ tool_evidence_available: false, usage: null }),
    result({ tool_calls: [raw] })
  ));
  await app.submit();
  assert.match(app.content('prompt'), /Not exposed by response/);
  assert.doesNotMatch(app.content('prompt'), /0 observed tool records/);
  assert.match(app.content('prompt'), /Not reported/);
  assert.match(app.content('hosted'), /1 observed tool record/);
  const pre = descendants(app.nodes.get('hosted-content')).find((node) => node.tagName === 'pre');
  assert.deepEqual(JSON.parse(pre.textContent), raw);
  assert.match(app.content('hosted'), /Reasoning tokens0/);
  const zero = setup(() => response());
  await zero.submit();
  assert.match(zero.content('prompt'), /0 observed tool records/);
});

test('qualifies truncated evidence counts and displays backend notes safely', async () => {
  const note = '<img src=x onerror=alert(1)>\nOnly a subset of native output items was retained.';
  const app = setup(() => response(
    result({ tool_calls: [{ type: 'search' }, { type: 'search_result' }], tool_evidence_truncated: true, tool_evidence_note: note }),
    result({ tool_calls: [{ type: 'search' }], tool_evidence_truncated: false, tool_evidence_note: 'Provider exposes only summary records.' })
  ));
  await app.submit();
  assert.match(app.content('prompt'), /At least 2 observed tool records/);
  assert.match(app.content('prompt'), /Tool evidence was truncated; additional records or details may be omitted/);
  assert.equal(descendants(app.nodes.get('prompt-content')).find((node) => node.textContent === note)?.tagName, 'p');
  assert.equal(descendants(app.nodes.get('prompt-content')).some((node) => node.tagName === 'img'), false);
  assert.match(app.content('hosted'), /1 observed tool record/);
  assert.match(app.content('hosted'), /Provider exposes only summary records/);
  assert.doesNotMatch(app.content('hosted'), /At least|was truncated/);
  assert.doesNotMatch(app.content('prompt') + app.content('hosted'), /\d+ tool calls?/);
});

test('unavailable or missing evidence never becomes a zero count, including truncated responses', async () => {
  for (const fields of [
    { tool_evidence_available: false },
    { tool_evidence_available: undefined },
    { tool_calls: undefined },
    { tool_calls: null }
  ]) {
    const app = setup(() => response(result({
      ...fields, tool_evidence_truncated: true, tool_evidence_note: 'Native evidence is unavailable.'
    })));
    await app.submit();
    assert.match(app.content('prompt'), /Not exposed by response/);
    assert.match(app.content('prompt'), /was truncated/);
    assert.match(app.content('prompt'), /Native evidence is unavailable/);
    assert.doesNotMatch(app.content('prompt'), /\d+ observed tool records?/);
  }
  const app = setup(() => response(result({ tool_evidence_truncated: true })));
  await app.submit();
  assert.match(app.content('prompt'), /At least 0 observed tool records/);
});

test('keeps all correlation IDs as safe text in native expandable diagnostics even for failed results', async () => {
  const traceId = '<script>trace-id</script>';
  const app = setup(() => response(
    result({ conversation_id: 'prompt-conversation', trace_id: traceId, response_id: 'prompt-response', span_id: 'prompt-span', continuation: 'not-for-display' }),
    result({ error: 'Agent failed', conversation_id: 'hosted-conversation', trace_id: 'hosted-trace', response_id: 'hosted-response', span_id: 'hosted-span' })
  ));
  await app.submit();
  for (const agent of ['prompt', 'hosted']) {
    const details = descendants(app.nodes.get(`${agent}-content`)).find((node) => node.className === 'diagnostics');
    assert.equal(details.tagName, 'details');
    assert.equal(details.hidden, false);
    assert.equal(details.attributes.open, undefined);
    assert.notEqual(details.open, true);
    assert.equal(details.children[0].tagName, 'summary');
    assert.equal(details.children[0].textContent, 'Conversation & telemetry');
    assert.equal(details.attributes['aria-labelledby'], `${agent}-correlation-heading`);
    assert.match(details.textContent, /Foundry conversation/);
    assert.match(details.textContent, /Foundry agent response/);
    assert.match(details.textContent, /UI invocation · App Insights operation \/ trace ID/);
    assert.match(details.textContent, /UI invocation span ID/);
    assert.match(details.textContent, new RegExp(`${agent}-conversation`));
    assert.match(details.textContent, new RegExp(`${agent}-span`));
    assert.doesNotMatch(details.textContent, /comparison-123|not-for-display/);
    assert.equal(descendants(details).some((node) => node.tagName === 'script'), false);
  }
  assert.match(app.content('prompt'), /<script>trace-id<\/script>/);
  assert.match(app.content('prompt'), /prompt-response/);
  assert.match(app.content('hosted'), /hosted-trace/);
  assert.match(app.content('hosted'), /hosted-response/);
  assert.equal(app.nodes.get('comparison-id').textContent, 'Comparison ID: comparison-123');
  assert.doesNotMatch(app.content('prompt'), /hosted-trace|hosted-response|not-for-display/);
  app.reset();
  assert.doesNotMatch(app.content('prompt') + app.content('hosted'), /Conversation & telemetry|trace-id|hosted-trace/);
});

test('prioritizes answers and visible evidence over collapsed diagnostics without losing details', async () => {
  const app = setup(() => response(
    result({ conversation_id: 'prompt-conversation', tool_calls: [{ type: 'search', query: 'example' }] }),
    result({ tool_evidence_available: false })
  ));
  await app.submit();
  for (const agent of ['prompt', 'hosted']) {
    const children = app.nodes.get(`${agent}-content`).children;
    assert.equal(children[0].className, 'answer-section');
    assert.equal(children.at(-1).className, 'diagnostics');
    assert.equal(children.at(-1).tagName, 'details');
    for (const name of ['metrics', 'evidence-section']) {
      const section = children.find((node) => node.className === name);
      assert.ok(section, `${name} must remain outside collapsed diagnostics`);
      assert.notEqual(section.tagName, 'details');
      assert.equal(section.hidden, false);
    }
  }
  const evidence = app.nodes.get('prompt-content').children.find((node) => node.className === 'evidence-section');
  const record = evidence.children.find((node) => node.tagName === 'details');
  assert.equal(record.children[0].tagName, 'summary');
  assert.match(record.children[0].textContent, /Tool record 1 · search/);
  assert.deepEqual(JSON.parse(record.children[1].textContent), { type: 'search', query: 'example' });
  assert.match(app.content('hosted'), /Not exposed by response/);
  assert.doesNotMatch(app.content('hosted'), /0 observed tool records/);
});

test('reset restores both neutral empty states and removes previous evidence and diagnostics', async () => {
  const app = setup(() => response(result({ text: 'Previous answer', conversation_id: 'previous-id' })));
  await app.submit();
  app.reset();
  for (const [agent, number] of [['prompt', '01'], ['hosted', '02'], ['prompt_none', '03'], ['hosted_none', '04']]) {
    const empty = app.nodes.get(`${agent}-content`).children[0];
    assert.equal(empty.className, 'empty-state');
    assert.equal(empty.children[0].textContent, number);
    assert.equal(empty.children[0].attributes['aria-hidden'], 'true');
    assert.match(empty.textContent, /Ready for your question/);
    assert.match(empty.textContent, /available search evidence/);
    assert.doesNotMatch(empty.textContent, /Previous answer|previous-id|Conversation & telemetry/);
    assert.equal(app.nodes.get(`${agent}-state`).textContent, 'Ready');
  }
  assert.equal(app.nodes.get('message').focused, true);
});

test('displays Not reported for missing, null, blank, and invalid IDs without inventing values or copy controls', async () => {
  const app = setup(() => response(
    result({ trace_id: null, response_id: '  ', span_id: 123, tool_evidence_note: null }),
    result({ response_id: 'response-only' })
  ));
  await app.submit();
  for (const agent of ['prompt', 'hosted']) {
    const block = descendants(app.nodes.get(`${agent}-content`)).find((node) => node.className === 'diagnostics');
    assert.equal((block.textContent.match(/Not reported/g) || []).length, agent === 'prompt' ? 4 : 6);
    assert.equal(descendants(block).filter((node) => node.tagName === 'button').length, agent === 'prompt' ? 0 : 1);
  }
  assert.match(app.content('hosted'), /Foundry agent responseresponse-only/);
});

test('copies only the selected actual ID with accessible controls and visible success feedback', async () => {
  const copied = [];
  const app = setup(() => response(result({
    conversation_id: 'conversation-actual',
    response_id: 'response-actual',
    trace_id: 'trace-actual',
    span_id: 'span-actual',
    continuation: 'opaque-must-not-copy'
  })), { clipboard: { writeText: async (text) => { copied.push(text); } } });
  await app.submit();
  const block = descendants(app.nodes.get('prompt-content')).find((node) => node.className === 'diagnostics');
  const buttons = descendants(block).filter((node) => node.tagName === 'button');
  const feedback = descendants(block).find((node) => node.className === 'copy-status');
  assert.equal(buttons.length, 4);
  for (const button of buttons) {
    assert.equal(button.type, 'button');
    assert.match(button.attributes['aria-label'], /^Copy .+ for Prompt agent$/);
    await button.listeners.click();
    assert.match(feedback.textContent, /copied\./);
    assert.equal(button.disabled, false);
  }
  assert.equal(feedback.attributes.role, 'status');
  assert.equal(feedback.attributes['aria-live'], 'polite');
  assert.deepEqual(copied, ['conversation-actual', 'response-actual', 'trace-actual', 'span-actual']);
  assert.doesNotMatch(block.textContent, /opaque-must-not-copy/);
});

test('clipboard unavailability or rejection produces visible manual-copy guidance, not silent success', async () => {
  for (const navigator of [{}, { clipboard: { writeText: async () => { throw new Error('Permission denied'); } } }]) {
    const app = setup(() => response(result({ trace_id: 'actual-trace' })), navigator);
    await app.submit();
    const block = descendants(app.nodes.get('prompt-content')).find((node) => node.className === 'diagnostics');
    const button = descendants(block).find((node) => node.tagName === 'button');
    await button.listeners.click();
    assert.match(block.textContent, /Could not copy UI invocation · App Insights operation \/ trace ID. Select and copy the value manually/);
    assert.doesNotMatch(block.textContent, /ID copied/);
    assert.equal(button.disabled, false);
  }
});

test('network errors and missing agent results show correlation placeholders rather than stale IDs', async () => {
  for (const failure of [
    () => { throw new TypeError('Offline'); },
    () => response(null, null)
  ]) {
    const app = setup((count) => count === 1
      ? response(result({ trace_id: 'prior-trace' }), result({ conversation_id: 'prior-conversation' }))
      : failure());
    await app.submit('First');
    await app.submit('Second');
    for (const agent of ['prompt', 'hosted']) {
      const block = descendants(app.nodes.get(`${agent}-content`)).find((node) => node.className === 'diagnostics');
      assert.equal((block.textContent.match(/Not reported/g) || []).length, agent === 'prompt' ? 4 : 7);
      assert.doesNotMatch(block.textContent, /prior-trace|prior-conversation/);
    }
  }
});

test('distinguishes actual UI invocation IDs from hosted runtime IDs and copies the correct source', async () => {
  const copied = [];
  const app = setup(() => response(result(), result({
    trace_id: 'ui-operation',
    span_id: 'ui-span',
    hosted_runtime_trace_id: 'runtime-operation',
    hosted_runtime_span_id: 'runtime-span',
    continuation: 'opaque-private-state'
  })), { clipboard: { writeText: async (value) => { copied.push(value); } } });
  await app.submit();
  const block = descendants(app.nodes.get('hosted-content')).find((node) => node.className === 'diagnostics');
  assert.match(block.textContent, /UI invocation · App Insights operation \/ trace IDui-operation/);
  assert.match(block.textContent, /UI invocation span IDui-span/);
  assert.match(block.textContent, /Hosted runtime · App Insights operation \/ trace IDruntime-operation/);
  assert.match(block.textContent, /Hosted runtime span IDruntime-span/);
  assert.doesNotMatch(app.content('prompt'), /Hosted runtime|runtime-operation|runtime-span/);
  const buttons = descendants(block).filter((node) => node.tagName === 'button');
  for (const button of buttons) {
    assert.match(button.attributes['aria-label'], /for Hosted agent$/);
    await button.listeners.click();
  }
  assert.deepEqual(copied, ['ui-operation', 'ui-span', 'runtime-operation', 'runtime-span']);
  assert.doesNotMatch(block.textContent, /opaque-private-state|comparison-123/);
});

test('does not substitute UI invocation IDs for unavailable hosted runtime IDs', async () => {
  const app = setup(() => response(result(), result({
    trace_id: 'ui-only', span_id: 'ui-span-only',
    hosted_runtime_trace_id: null, hosted_runtime_span_id: null
  })));
  await app.submit();
  assert.match(app.content('hosted'), /Hosted runtime · App Insights operation \/ trace IDNot reported/);
  assert.match(app.content('hosted'), /Hosted runtime span IDNot reported/);
});

test('shows conversation scope and safe backend notes without manufacturing a conversation ID', async () => {
  const note = '<img src=x onerror=alert(1)>\nConversation resource unavailable for this route.';
  const app = setup(() => response(
    result({ conversation_id: null, conversation_note: note }),
    result({ conversation_id: 'upstream-conversation', conversation_scope: 'hosted_model', conversation_note: 'Stored in the upstream model conversation.' })
  ));
  await app.submit();
  assert.match(app.content('prompt'), /Foundry conversationNot reported/);
  assert.ok(app.content('prompt').includes(note));
  assert.equal(descendants(app.nodes.get('prompt-content')).some((node) => node.tagName === 'img'), false);
  assert.match(app.content('hosted'), /Hosted model conversationupstream-conversation/);
  assert.match(app.content('hosted'), /Stored in the upstream model conversation/);
  const copy = descendants(app.nodes.get('hosted-content')).find((node) => node.tagName === 'button');
  assert.equal(copy.attributes['aria-label'], 'Copy Hosted model conversation for Hosted agent');
  const absent = setup(() => response(result({ conversation_scope: 'unknown', conversation_note: null })));
  await absent.submit();
  assert.match(absent.content('prompt'), /Foundry conversationNot reported/);
  assert.doesNotMatch(absent.content('prompt'), /Hosted model conversation|undefined/);
});

test('shows safe per-side telemetry notes without inferring IDs or exposing private continuation', async () => {
  const note = '<script>untrusted-note</script>\nRuntime telemetry was not returned.';
  const app = setup(() => response(
    result({ telemetry_note: null, conversation_note: null }),
    result({ error: 'Invocation failed', telemetry_note: note, continuation: 'private-token' })
  ));
  await app.submit();
  const prompt = descendants(app.nodes.get('prompt-content')).find((node) => node.className === 'diagnostics');
  const hosted = descendants(app.nodes.get('hosted-content')).find((node) => node.className === 'diagnostics');
  assert.equal(descendants(prompt).filter((node) => node.className === 'evidence-note').length, 0);
  assert.ok(hosted.textContent.includes(note));
  assert.equal(descendants(hosted).some((node) => node.tagName === 'script'), false);
  assert.equal((hosted.textContent.match(/Not reported/g) || []).length, 7);
  assert.doesNotMatch(hosted.textContent, /private-token/);
  assert.doesNotMatch(prompt.textContent, /untrusted-note/);
  app.reset();
  assert.doesNotMatch(app.content('hosted'), /untrusted-note/);
});

test('shows hosted runtime telemetry notes separately without manufacturing IDs or exposing tokens', async () => {
  const note = '<script>runtime-note</script>\nRuntime span was not recording.';
  const app = setup(() => response(
    result({ hosted_runtime_telemetry_note: 'Not applicable to the prompt panel' }),
    result({
      error: 'This token was already used. Start a new comparison.',
      telemetry_note: 'UI invocation telemetry is unavailable.',
      hosted_runtime_telemetry_note: note,
      hosted_runtime_trace_id: null,
      hosted_runtime_span_id: null,
      continuation: 'must-remain-private'
    })
  ));
  await app.submit();
  const block = descendants(app.nodes.get('hosted-content')).find((node) => node.className === 'diagnostics');
  assert.ok(block.textContent.includes(`Hosted runtime telemetry: ${note}`));
  assert.match(block.textContent, /UI invocation telemetry is unavailable/);
  assert.match(block.textContent, /Hosted runtime span IDNot reported/);
  assert.equal(descendants(block).some((node) => node.tagName === 'script'), false);
  assert.doesNotMatch(block.textContent, /must-remain-private/);
  assert.doesNotMatch(app.content('prompt'), /Not applicable|Hosted runtime telemetry:/);
  assert.match(app.content('hosted'), /This token was already used. Start a new comparison/);
  assert.equal(app.requests.length, 1);
  const absent = setup(() => response(result(), result({ hosted_runtime_telemetry_note: null })));
  await absent.submit();
  assert.doesNotMatch(absent.content('hosted'), /Hosted runtime telemetry:/);
});

test('labels and copies invoked agent and hosted model response IDs independently', async () => {
  const copied = [];
  const app = setup(() => response(
    result({ response_id: 'resp_prompt', model_response_id: 'not-a-prompt-row' }),
    result({ response_id: 'resp_outer', model_response_id: 'resp_model', continuation: 'private-capability' })
  ), { clipboard: { writeText: async (value) => { copied.push(value); } } });
  await app.submit();
  assert.match(app.content('prompt'), /Foundry agent responseresp_prompt/);
  assert.doesNotMatch(app.content('prompt'), /Hosted model response|not-a-prompt-row/);
  const block = descendants(app.nodes.get('hosted-content')).find((node) => node.className === 'diagnostics');
  const rows = descendants(block).find((node) => node.tagName === 'dl').children;
  const agentRow = rows.find((node) => node.children[0].textContent === 'Foundry agent response');
  const modelRow = rows.find((node) => node.children[0].textContent === 'Hosted model response');
  assert.equal(agentRow.children[1].children[0].textContent, 'resp_outer');
  assert.equal(modelRow.children[1].children[0].textContent, 'resp_model');
  for (const row of [agentRow, modelRow]) {
    const button = descendants(row).find((node) => node.tagName === 'button');
    assert.equal(button.attributes['aria-label'], `Copy ${row.children[0].textContent} for Hosted agent`);
    await button.listeners.click();
  }
  assert.deepEqual(copied, ['resp_outer', 'resp_model']);
  assert.doesNotMatch(block.textContent, /private-capability/);
});

test('never falls back to agent response ID when the hosted model response ID is missing or invalid', async () => {
  for (const modelResponseId of [undefined, null, '', '  ', 123, { id: 'not-a-string' }]) {
    const app = setup(() => response(result(), result({
      response_id: 'resp_outer',
      model_response_id: modelResponseId
    })));
    await app.submit();
    const block = descendants(app.nodes.get('hosted-content')).find((node) => node.className === 'diagnostics');
    const rows = descendants(block).find((node) => node.tagName === 'dl').children;
    const modelRow = rows.find((node) => node.children[0].textContent === 'Hosted model response');
    assert.equal(modelRow.children[1].textContent, 'Not reported');
    assert.equal(descendants(modelRow).some((node) => node.tagName === 'button'), false);
    assert.match(block.textContent, /Foundry agent responseresp_outer/);
  }
});

test('renders and copies actual hosted model response ID as inert text even in failed results', async () => {
  const modelId = '<script>resp_model</script>';
  const copied = [];
  const app = setup(() => response(result(), result({
    text: null, error: 'Agent failed', response_id: null, model_response_id: modelId
  })), { clipboard: { writeText: async (value) => { copied.push(value); } } });
  await app.submit();
  const block = descendants(app.nodes.get('hosted-content')).find((node) => node.className === 'diagnostics');
  assert.match(block.textContent, /Foundry agent responseNot reported/);
  assert.ok(block.textContent.includes(`Hosted model response${modelId}`));
  assert.equal(descendants(block).some((node) => node.tagName === 'script'), false);
  await descendants(block).find((node) => node.tagName === 'button').listeners.click();
  assert.deepEqual(copied, [modelId]);
});

test('renders untrusted answers as inert text, never executing embedded markup', async () => {
  const payload = '<script>alert("untrusted")</script>';
  const app = setup(() => response(result({ text: payload })));
  await app.submit();
  const nodes = descendants(app.nodes.get('prompt-content'));
  assert.match(app.content('prompt'), /<script>/);
  assert.equal(nodes.some((node) => node.tagName === 'script'), false);
});

test('rejects empty and oversized questions and disallows duplicate in-flight requests', async () => {
  let resolve;
  const app = setup(() => new Promise((done) => { resolve = done; }));
  await app.submit(' \n ');
  await app.submit('a'.repeat(4001));
  assert.equal(app.requests.length, 0);
  assert.equal(app.nodes.get('form-error').hidden, false);
  const pending = app.submit('A valid question');
  assert.equal(app.nodes.get('submit').disabled, true);
  assert.equal(app.nodes.get('reset').disabled, true);
  assert.equal(app.nodes.get('message').readOnly, true);
  assert.equal(app.nodes.get('prompt-panel').attributes['aria-busy'], 'true');
  await app.submit('Duplicate');
  assert.equal(app.requests.length, 1);
  resolve(response());
  await pending;
  assert.equal(app.nodes.get('submit').disabled, false);
  assert.equal(app.nodes.get('hosted-panel').attributes['aria-busy'], 'false');
});

test('handles network, HTTP, and invalid JSON failures in both panels without saving failed turns', async () => {
  const failures = [
    () => { throw new TypeError('Network offline'); },
    () => ({ ok: false, status: 503 }),
    () => ({ ok: true, json: async () => { throw new SyntaxError('Bad JSON'); } }),
    () => ({ ok: true, json: async () => null })
  ];
  for (const failure of failures) {
    const app = setup((count) => count === 1 ? failure() : response());
    await app.submit('Failed turn');
    for (const agent of ['prompt', 'hosted']) {
      assert.equal(app.nodes.get(`${agent}-state`).textContent, 'Error');
      assert.match(app.content(agent), /not added/);
    }
    assert.equal(app.nodes.get('submit').disabled, false);
    assert.equal(app.nodes.get('message').value, 'Failed turn');
    await app.submit('Retry');
    assert.deepEqual(app.requests[1].body.history, { prompt: [], hosted: [], prompt_none: [], hosted_none: [] });
  }
});

test('missing agent results do not discard the other successful answer', async () => {
  const app = setup(() => response(result(), null));
  await app.submit();
  assert.equal(app.nodes.get('prompt-state').textContent, 'Complete');
  assert.equal(app.nodes.get('hosted-state').textContent, 'Error');
  await app.submit('Follow-up');
  assert.equal(app.requests[1].body.history.prompt.length, 2);
  assert.equal(app.requests[1].body.history.hosted.length, 0);
});

test('has no reasoning control; each panel labels its fixed reasoning effort and renders as a 2x2 grid', async () => {
  assert.doesNotMatch(html, /id="reasoning"/);
  assert.doesNotMatch(html, /reasoning-control/);
  assert.match(html, /01 \/ Prompt · Low reasoning/);
  assert.match(html, /02 \/ Hosted · Low reasoning/);
  assert.match(html, /03 \/ Prompt · No reasoning/);
  assert.match(html, /04 \/ Hosted · No reasoning/);
  const app = setup(() => response());
  await app.submit();
  assert.deepEqual(Object.keys(app.requests[0].body).sort(), ['continuation', 'history', 'message', 'session']);
  assert.deepEqual(Object.keys(app.requests[0].body.history).sort(), ['hosted', 'hosted_none', 'prompt', 'prompt_none']);
  assert.deepEqual(Object.keys(app.requests[0].body.session).sort(), ['hosted', 'hosted_none']);
});

test('sends a stable per-chat hosted session id across turns, and a fresh one after reset', async () => {
  const app = setup(() => response());
  await app.submit();
  const firstSession = app.requests[0].body.session;
  assert.equal(typeof firstSession.hosted, 'string');
  assert.equal(typeof firstSession.hosted_none, 'string');
  assert.notEqual(firstSession.hosted, firstSession.hosted_none);
  await app.submit();
  assert.deepEqual(app.requests[1].body.session, firstSession);
  app.reset();
  await app.submit();
  assert.notEqual(app.requests[2].body.session.hosted, firstSession.hosted);
  assert.notEqual(app.requests[2].body.session.hosted_none, firstSession.hosted_none);
});

test('sends opaque continuations independently, advances successful sides only, and clears both on reset', async () => {
  const promptToken = 'opaque.prompt/+==<do-not-interpret>';
  const hostedToken = 'opaque.hosted/+==<do-not-interpret>';
  const app = setup((count) => {
    if (count === 1) return response(
      result({ continuation: promptToken }), result({ continuation: hostedToken })
    );
    if (count === 2) return response(
      result({ continuation: 'prompt-next' }),
      result({ error: 'Failed hosted turn', continuation: 'must-not-save' })
    );
    return response(result({ continuation: 'prompt-latest' }), result({ continuation: 'hosted-next' }));
  });
  await app.submit('First');
  assert.deepEqual(app.requests[0].body.continuation, { prompt: null, hosted: null, prompt_none: null, hosted_none: null });
  assert.equal(app.content('prompt').includes(promptToken), false);
  assert.equal(app.content('hosted').includes(hostedToken), false);
  await app.submit('Second');
  assert.deepEqual(app.requests[1].body.continuation, { prompt: promptToken, hosted: hostedToken, prompt_none: null, hosted_none: null });
  await app.submit('Third');
  assert.deepEqual(app.requests[2].body.continuation, { prompt: 'prompt-next', hosted: hostedToken, prompt_none: null, hosted_none: null });
  assert.equal(app.requests[2].body.history.prompt.length, 4);
  assert.equal(app.requests[2].body.history.hosted.length, 2);
  app.reset();
  await app.submit('Fresh');
  assert.deepEqual(app.requests[3].body.continuation, { prompt: null, hosted: null, prompt_none: null, hosted_none: null });
  assert.deepEqual(app.requests[3].body.history, { prompt: [], hosted: [], prompt_none: [], hosted_none: [] });
});

test('clears stale continuation when a successful response returns null, missing, or invalid values', async () => {
  for (const fields of [{ continuation: null }, {}, { continuation: { invalid: 'token' } }]) {
    const app = setup((count) => count === 1
      ? response(result({ continuation: 'prompt-token' }), result({ continuation: 'hosted-token' }))
      : response(result(fields), result(fields)));
    await app.submit('First');
    await app.submit('Second');
    await app.submit('Third');
    assert.deepEqual(app.requests[2].body.continuation, { prompt: null, hosted: null, prompt_none: null, hosted_none: null });
    assert.equal(app.requests[2].body.history.prompt.length, 4);
    assert.equal(app.requests[2].body.history.hosted.length, 4);
  }
});

test('preserves prior continuations and histories after network failure or missing answer text', async () => {
  const app = setup((count) => {
    if (count === 2) throw new TypeError('Network offline');
    if (count === 3) return response(result({ text: '', continuation: 'must-not-save' }), null);
    return response(result({ continuation: 'prompt-token' }), result({ continuation: 'hosted-token' }));
  });
  await app.submit('First');
  await app.submit('Network failure');
  await app.submit('Missing answers');
  await app.submit('Retry');
  for (const request of app.requests.slice(1)) {
    assert.deepEqual(request.body.continuation, { prompt: 'prompt-token', hosted: 'hosted-token', prompt_none: null, hosted_none: null });
    assert.equal(request.body.history.prompt.length, 2);
    assert.equal(request.body.history.hosted.length, 2);
  }
});
