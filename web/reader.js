/** Continuous PDF pages with a native selectable text layer and PDF-backed endpoints. */
export class ContinuousReader {
  constructor({ api, onSelection, onPage, onError, onPending }) {
    Object.assign(this, { api, onSelection, onPage, onError, onPending });
    this.stage = document.getElementById('paperStage');
    this.scroll = document.getElementById('paperScroll');
    this.mode = 'text';
    this.epoch = 0;
    this.ticket = 0;
    this.scroll.addEventListener('scroll', () => {
      if (this.scrollFrame) return;
      this.scrollFrame = requestAnimationFrame(() => {
        this.scrollFrame = null;
        const y = this.scroll.getBoundingClientRect().top + 80;
        const pages = [...this.stage.children];
        const page = pages.find(p => p.getBoundingClientRect().bottom > y);
        if (page) this.onPage(Number(page.dataset.page));
      });
    });
    new ResizeObserver(() => this.resize()).observe(this.stage);
    this.stage.addEventListener('pointerdown', e => this.down(e));
    this.stage.addEventListener('pointermove', e => this.move(e));
    this.stage.addEventListener('pointercancel', () => { this.drag = null; this.selecting = false; });
    document.addEventListener('pointerup', e => this.up(e));
    document.addEventListener('keyup', e => {
      if (this.mode === 'text' && (e.shiftKey || e.key === 'Shift')) {
        clearTimeout(this.keyTimer);
        this.keyTimer = setTimeout(() => this.capture(e.key === 'Shift'), 180);
      }
    });
  }
  async load(doc) {
    const epoch = ++this.epoch;
    ++this.ticket;
    this.lastRange = '';
    this.drag = null;
    this.doc = doc;
    this.stage.replaceChildren();
    this.stage.style.display = 'block';
    this.stage.dataset.mode = this.mode;
    const layout = await this.api(`/documents/${doc.id}/text-layer`);
    if (epoch !== this.epoch) return;
    const measure = document.createElement('canvas').getContext('2d');
    for (const page of layout) {
      const sheet = document.createElement('section');
      sheet.className = 'paper-page';
      sheet.dataset.page = page.page;
      sheet.dataset.width = page.width;
      const label = document.createElement('div');
      label.className = 'paper-page-label';
      label.textContent = `第 ${page.page} 页`;
      const canvas = document.createElement('div');
      canvas.className = 'page-canvas';
      canvas.style.aspectRatio = `${page.width}/${page.height}`;
      const image = document.createElement('img');
      image.src = `/api/documents/${doc.id}/pages/${page.page}.png`;
      image.alt = `${doc.title} 第 ${page.page} 页`;
      image.loading = 'lazy'; image.decoding = 'async'; image.draggable = false;
      const layer = document.createElement('div');
      layer.className = 'text-layer';
      const blocks = document.createElement('div');
      blocks.className = 'block-layer';
      for (const [index, line] of page.lines.entries()) {
        const span = document.createElement('span');
        span.className = 'pdf-text-line';
        span.dataset.page = page.page; span.dataset.line = index;
        span.textContent = line.text;
        const family = /Courier|Mono/i.test(line.font) ? 'monospace' : /Helvetica|Arial|Sans/i.test(line.font) ? 'sans-serif' : '"Times New Roman", serif';
        const weight = /Bold/i.test(line.font) ? 'bold' : 'normal';
        const style = /Italic|Oblique/i.test(line.font) ? 'italic' : 'normal';
        measure.font = `${style} ${weight} ${line.size}px ${family}`;
        const width = measure.measureText(line.text).width;
        this.position(span, line.bbox, page);
        Object.assign(span.style, { fontFamily: family, fontWeight: weight, fontStyle: style,
          fontSize: `calc(var(--page-scale) * ${line.size}px)`,
          transform: `scaleX(${width ? (line.bbox[2] - line.bbox[0]) / width : 1})` });
        layer.append(span);
      }
      for (const block of doc.pages[page.page - 1].blocks) {
        const button = document.createElement('button');
        button.className = 'block'; button.dataset.id = block.id;
        button.setAttribute('aria-label', `引用段落：${block.text.slice(0, 90)}`);
        this.position(button, block.bbox, page);
        button.onclick = () => this.onSelection({ page: page.page, block_id: block.id, text: block.text });
        blocks.append(button);
      }
      const region = document.createElement('div');
      region.className = 'region-box'; region.hidden = true;
      canvas.append(image, layer, blocks, region);
      sheet.append(label, canvas); this.stage.append(sheet);
    }
    this.resize();
  }
  position(node, [x0, y0, x1, y1], page) {
    Object.assign(node.style, { left: `${x0 / page.width * 100}%`, top: `${y0 / page.height * 100}%`,
      width: `${(x1 - x0) / page.width * 100}%`, height: `${(y1 - y0) / page.height * 100}%` });
  }
  resize() {
    for (const page of this.stage.children) {
      const width = page.querySelector('.page-canvas').getBoundingClientRect().width;
      if (width) page.style.setProperty('--page-scale', width / Number(page.dataset.width));
    }
  }
  goto(number) {
    if (!this.doc) return;
    number = Math.max(1, Math.min(this.doc.page_count, Number(number) || 1));
    const sheet = this.stage.querySelector(`[data-page="${number}"]`);
    if (sheet) this.scroll.scrollTop += sheet.getBoundingClientRect().top - this.scroll.getBoundingClientRect().top;
    this.onPage(number);
  }
  setMode(value) {
    ++this.ticket;
    this.mode = value; this.stage.dataset.mode = value;
    this.drag = null; this.selecting = false;
  }
  paint(selection) {
    for (const b of this.stage.querySelectorAll('.block')) b.classList.toggle('selected', b.dataset.id === selection?.block_id);
  }
  async capture(auto = true) {
    if (this.mode !== 'text' || !this.doc) return;
    const selected = window.getSelection();
    if (!selected?.rangeCount || selected.isCollapsed) return;
    const range = selected.getRangeAt(0);
    // Both ends must belong to this PDF, never collect surrounding UI or another document.
    if (!this.stage.contains(range.startContainer) || !this.stage.contains(range.endContainer)) return;
    const touched = [];
    for (const line of this.stage.querySelectorAll('.pdf-text-line')) {
      if (!range.intersectsNode(line)) continue;
      const text = line.firstChild;
      let a = range.startContainer === text ? range.startOffset : 0;
      let z = range.endContainer === text ? range.endOffset : text.length;
      if (range.startContainer === line) a = range.startOffset ? text.length : 0;
      if (range.endContainer === line) z = range.endOffset ? text.length : 0;
      if (z > a) touched.push({ line, a, z });
    }
    if (!touched.length) return;
    const endpoint = (item, offset) => ({ page: Number(item.line.dataset.page), line: Number(item.line.dataset.line), offset });
    const first = touched[0], last = touched[touched.length - 1];
    const text_range = { start: endpoint(first, first.a), end: endpoint(last, last.z) };
    const key = JSON.stringify(text_range);
    if (key === this.lastRange && auto) return;
    const ticket = ++this.ticket, epoch = this.epoch, docId = this.doc.id;
    this.onPending();
    try {
      const result = await this.api('/selections', { method: 'POST', body: JSON.stringify({ document_id: docId, page: first.line.dataset.page * 1, text_range }) });
      if (ticket !== this.ticket || epoch !== this.epoch) return;
      this.lastRange = auto ? key : '';
      this.onSelection({ ...result, block_id: null, bbox: null }, auto);
    } catch (error) {
      if (ticket === this.ticket) this.onError(error.message);
    }
  }
  point(event, canvas) {
    const rect = canvas.getBoundingClientRect();
    return [Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)), Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height))];
  }
  down(e) {
    if (this.mode === 'text') { this.selecting = true; return; }
    if (this.mode !== 'region' || e.button !== 0) return;
    const canvas = e.target.closest('.page-canvas');
    if (!canvas) return;
    const sheet = canvas.closest('.paper-page');
    this.drag = { canvas, page: Number(sheet.dataset.page), start: this.point(e, canvas) };
    canvas.setPointerCapture(e.pointerId);
    this.move(e);
  }
  move(e) {
    if (!this.drag) return;
    const { canvas, start } = this.drag, p = this.point(e, canvas);
    const box = canvas.querySelector('.region-box'); box.hidden = false;
    Object.assign(box.style, { left: `${Math.min(start[0], p[0]) * 100}%`, top: `${Math.min(start[1], p[1]) * 100}%`, width: `${Math.abs(start[0] - p[0]) * 100}%`, height: `${Math.abs(start[1] - p[1]) * 100}%` });
  }
  up(e) {
    if (this.selecting) {
      this.selecting = false;
      requestAnimationFrame(() => this.capture());
    }
    if (!this.drag) return;
    const { canvas, start, page: number } = this.drag, p = this.point(e, canvas);
    this.drag = null;
    const page = this.doc.pages[number - 1];
    const bbox = [Math.min(start[0], p[0]) * page.width, Math.min(start[1], p[1]) * page.height, Math.max(start[0], p[0]) * page.width, Math.max(start[1], p[1]) * page.height];
    if (bbox[2] - bbox[0] < 8 || bbox[3] - bbox[1] < 8) {
      canvas.querySelector('.region-box').hidden = true;
      return this.onError('框选范围太小，请重新选择');
    }
    this.onSelection({ page: number, bbox, text: '已框选图表 / 公式区域，将结合页面图像和上下文解读' });
  }
}
