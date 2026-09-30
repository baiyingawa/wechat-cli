const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
    constructor() {
        this.children = [];
        this.textContent = '';
        this.classList = { add() {}, toggle() {} };
    }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    querySelectorAll() { return []; }
}

const nodes = new Map();
const attack = '<img src=x onerror="globalThis.executed=true">';
const tasks = [{id: attack, method: attack, status: 'queued', context: attack,
    duplicate_of: attack, created_at: 0, phases: {[attack]: {status: attack}}}];
const document = {
    createElement: () => new Element(),
    getElementById(id) {
        if (!nodes.has(id)) nodes.set(id, new Element());
        return nodes.get(id);
    },
    querySelectorAll: () => [],
};
const box = document.getElementById('tasks');
Object.defineProperty(box, 'innerHTML', {set() {throw Error('Task data must never enter innerHTML');}});
const method = {method: 'session.status', status: 'implemented', params_schema: {required: [], properties: {}}};
const context = vm.createContext({document, setInterval() {}, fetch: async route => ({
    ok: true, json: async () => route === '/api/capabilities'
        ? {methods: [method]} : {tasks, active_context: {context: attack, reset_due_at: 0}}
})});
const page = fs.readFileSync(path.join(__dirname, '../src/wechat_cli/static/index.html'), 'utf8');
vm.runInContext(page.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1], context);
(async () => {
    await vm.runInContext('loadTasks()', context);
    assert.equal(box.children.length, 2);
    assert.equal(box.children[0].children[0].textContent, attack);
    assert.ok(box.children[0].children[2].textContent.includes(attack));
    assert.ok(box.children[0].children[3].textContent.includes(attack));
    assert.ok(box.children[1].textContent.includes(attack));
    assert.equal(context.executed, undefined);
    assert.equal(vm.runInContext(`escapeHtml(${JSON.stringify(attack)})`, context),
        '&lt;img src=x onerror=&quot;globalThis.executed=true&quot;&gt;');
    console.log('Demo DOM XSS regression checks passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
