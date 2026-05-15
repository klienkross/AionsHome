/* ── Aion Web Components ── */

class AionDots extends HTMLElement {
  static get observedAttributes() { return ['color']; }

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: inline-flex; gap: 3px; align-items: center; }
        span {
          width: 5px; height: 5px; border-radius: 50%;
          background: var(--dot-color, var(--accent, #ff8359));
          animation: bounce 1.4s infinite ease-in-out both;
        }
        span:nth-child(2) { animation-delay: 0.16s; }
        span:nth-child(3) { animation-delay: 0.32s; }
        @keyframes bounce {
          0%, 80%, 100% { transform: scale(0.4); opacity: 0.4; }
          40% { transform: scale(1); opacity: 1; }
        }
      </style>
      <span></span><span></span><span></span>
    `;
  }

  attributeChangedCallback(name, _, val) {
    if (name === 'color') this.shadowRoot.host.style.setProperty('--dot-color', val);
  }
}

class AionToggle extends HTMLElement {
  static get observedAttributes() { return ['label', 'checked']; }

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: flex; align-items: center; justify-content: space-between; cursor: pointer; }
        .label { font-size: 14px; font-weight: 600; color: var(--text, #4a3b32); }
        .track {
          position: relative; width: 44px; height: 24px; flex-shrink: 0;
          background: var(--surface2, #fff0e6); border: 1px solid var(--border, #f0e4dd);
          border-radius: 12px; transition: 0.2s;
        }
        .track.on { background: var(--accent, #ff8359); border-color: var(--accent, #ff8359); }
        .thumb {
          position: absolute; top: 2px; left: 2px;
          width: 18px; height: 18px; border-radius: 50%;
          background: var(--text3, #b0a39a); transition: 0.2s;
        }
        .track.on .thumb { transform: translateX(20px); background: #fff; }
      </style>
      <span class="label"></span>
      <div class="track"><div class="thumb"></div></div>
    `;
    this.shadowRoot.querySelector('.track').addEventListener('click', () => this._toggle());
    this.shadowRoot.querySelector('.label').addEventListener('click', () => this._toggle());
  }

  get checked() { return this.hasAttribute('checked'); }
  set checked(v) { v ? this.setAttribute('checked', '') : this.removeAttribute('checked'); }

  attributeChangedCallback(name, _, val) {
    if (name === 'label') this.shadowRoot.querySelector('.label').textContent = val;
    if (name === 'checked') this._render();
  }

  connectedCallback() { this._render(); }

  _render() {
    const track = this.shadowRoot.querySelector('.track');
    this.checked ? track.classList.add('on') : track.classList.remove('on');
  }

  _toggle() {
    this.checked = !this.checked;
    this.dispatchEvent(new CustomEvent('change', { detail: { checked: this.checked } }));
  }
}

class AionTopbar extends HTMLElement {
  static get observedAttributes() { return ['title', 'back']; }

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: flex; align-items: center; gap: 10px;
          padding: 12px 16px;
          border-bottom: 1px solid var(--border, #f0e4dd);
          background: rgba(255,249,245,0.85);
          backdrop-filter: blur(20px) saturate(1.6);
          -webkit-backdrop-filter: blur(20px) saturate(1.6);
          flex-shrink: 0; z-index: 10;
        }
        .back-btn {
          background: none; border: none; color: var(--accent, #ff8359);
          font-size: 20px; cursor: pointer; padding: 4px 6px; flex-shrink: 0;
          display: flex; align-items: center;
        }
        h2 {
          flex: 1; margin: 0; font-size: 17px; font-weight: 600;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
          color: var(--text, #4a3b32);
        }
        .actions { display: flex; align-items: center; gap: 6px; }
      </style>
      <button class="back-btn">⬅</button>
      <h2></h2>
      <div class="actions"><slot name="actions"></slot></div>
    `;
    this.shadowRoot.querySelector('.back-btn').addEventListener('click', () => {
      location.href = this.getAttribute('back') || '/';
    });
  }

  attributeChangedCallback(name, _, val) {
    if (name === 'title') this.shadowRoot.querySelector('h2').textContent = val;
  }

  connectedCallback() {
    this.shadowRoot.querySelector('h2').textContent = this.getAttribute('title') || '';
  }
}

class AionModal extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: none; }
        :host([visible]) { display: block; }
        .overlay {
          position: fixed; inset: 0;
          background: rgba(0,0,0,0.25); z-index: 1000;
          display: flex; align-items: center; justify-content: center;
          animation: overlayIn 0.2s ease;
        }
        @keyframes overlayIn { from { opacity: 0; } to { opacity: 1; } }
        .popup {
          position: relative;
          background: linear-gradient(145deg, #fffaf6, #fff3eb);
          border: 1px solid rgba(255,131,89,0.18);
          border-radius: 16px; padding: 22px 24px 20px;
          min-width: 260px; max-width: 380px; width: 85vw;
          box-shadow: 0 12px 40px rgba(0,0,0,0.12), 0 2px 8px rgba(255,131,89,0.08);
          animation: cardIn 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
        }
        @keyframes cardIn {
          from { opacity: 0; transform: scale(0.85) translateY(10px); }
          to { opacity: 1; transform: scale(1) translateY(0); }
        }
        .label-slot {
          font-size: 12px; color: #b0a39a;
          margin-bottom: 12px; letter-spacing: 1px;
        }
        .body-slot {
          font-size: 15px; color: #4a3b32;
          line-height: 1.7; white-space: pre-wrap; word-break: break-word;
        }
        .close-btn {
          position: absolute; top: 12px; right: 14px;
          background: none; border: none; color: #b0a39a;
          font-size: 18px; cursor: pointer; padding: 4px 6px;
          border-radius: 6px; line-height: 1;
          transition: color 0.15s, background 0.15s;
        }
        .close-btn:hover { color: #4a3b32; background: rgba(0,0,0,0.05); }
      </style>
      <div class="overlay">
        <div class="popup">
          <button class="close-btn">✕</button>
          <div class="label-slot"><slot name="label"></slot></div>
          <div class="body-slot"><slot name="body"></slot></div>
        </div>
      </div>
    `;
    this.shadowRoot.querySelector('.close-btn').addEventListener('click', () => this.close());
    this.shadowRoot.querySelector('.overlay').addEventListener('click', (e) => {
      if (e.target === e.currentTarget) this.close();
    });
  }

  open() { this.setAttribute('visible', ''); }

  close() {
    this.removeAttribute('visible');
    this.dispatchEvent(new Event('close'));
  }
}

class AionSidebarBtn extends HTMLElement {
  static get observedAttributes() { return ['icon']; }

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        button {
          width: 100%; padding: 10px; border: none; border-radius: 8px;
          background: var(--surface2, #fff0e6); color: var(--text, #4a3b32);
          font-size: 14px; cursor: pointer;
          display: flex; align-items: center; justify-content: center; gap: 6px;
          font-family: inherit;
        }
        button:hover { background: var(--border, #f0e4dd); }
        .icon { flex-shrink: 0; }
      </style>
      <button><span class="icon"></span><slot></slot></button>
    `;
    this.shadowRoot.querySelector('button').addEventListener('click', () => {
      this.click();
    });
  }

  attributeChangedCallback(name, _, val) {
    if (name === 'icon') this.shadowRoot.querySelector('.icon').textContent = val;
  }

  connectedCallback() {
    const icon = this.getAttribute('icon');
    if (icon) this.shadowRoot.querySelector('.icon').textContent = icon;
  }
}

class AionIconBtn extends HTMLElement {
  static get observedAttributes() { return ['icon']; }

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: inline-block; }
        button {
          width: 40px; height: 40px; border: none; border-radius: 8px;
          background: var(--surface2, #fff0e6); color: var(--text, #4a3b32);
          font-size: 18px; cursor: pointer;
          display: flex; align-items: center; justify-content: center;
          font-family: inherit; padding: 0;
        }
        button:hover { background: var(--border, #f0e4dd); }
      </style>
      <button></button>
    `;
    this.shadowRoot.querySelector('button').addEventListener('click', () => {
      this.click();
    });
  }

  attributeChangedCallback(name, _, val) {
    if (name === 'icon') this.shadowRoot.querySelector('button').textContent = val;
  }

  connectedCallback() {
    const icon = this.getAttribute('icon');
    if (icon) this.shadowRoot.querySelector('button').textContent = icon;
  }
}

customElements.define('aion-dots', AionDots);
customElements.define('aion-toggle', AionToggle);
customElements.define('aion-topbar', AionTopbar);
customElements.define('aion-modal', AionModal);
customElements.define('aion-sidebar-btn', AionSidebarBtn);
customElements.define('aion-icon-btn', AionIconBtn);
