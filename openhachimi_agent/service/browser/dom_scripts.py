"""JavaScript scripts for browser DOM manipulation and analysis."""

# 挑战评估核心逻辑（DETECT 与 OBSERVER 共用同一套规则，两份内联保持一致）：
#
# 误报背景：大量正常网站在表单里常驻 reCAPTCHA/Turnstile/hCaptcha 防刷组件，
# 这些 widget 安静地躺在 DOM 里（304x76 的小勾选框或干脆隐藏），用户看不到任何挑战。
# 旧逻辑"iframe src 含 recaptcha 即报验证"会把这类页面误判为拦截页并劫持 agent。
#
# 分级规则：
# - hard（真挑战，需人工）：整页插页（标题/短文本命中）、或大面积可见挑战框
#   （高 >= 300px 或宽 >= 480px：reCAPTCHA 图片九宫格 ~500x600、hCaptcha 弹窗 ~550、
#   geetest/易盾滑块 ~430x330、Cloudflare 整页插页全屏）
# - soft（组件在场，无挑战）：小 widget（高 < 300 且宽 < 480）或不可见的匹配项。
#   只作为提示信息（表单提交可能弹挑战），不阻断 agent 操作
_ASSESS_CHALLENGE_JS = """
    function isRendered(el) {
        if (!el.getClientRects().length) return false;
        const r = el.getBoundingClientRect();
        return r.width > 1 && r.height > 1;
    }
    function isLargeVisible(el) {
        if (!isRendered(el)) return false;
        const r = el.getBoundingClientRect();
        return r.height >= 300 || r.width >= 480;
    }
    function assessCaptcha(patterns) {
        if (!patterns) return null;

        // 1. 整页插页标题：hard
        const title = (document.title || '').toLowerCase();
        for (const p of patterns.titles) {
            if (title.includes(p)) return {level: 'hard', reason: 'title_match: ' + p};
        }

        // 2. 挑战域名 iframe：大面积可见 = hard，小组件/隐藏 = soft
        let soft = null;
        const iframes = Array.from(document.querySelectorAll('iframe'));
        for (const iframe of iframes) {
            const src = (iframe.src || '').toLowerCase();
            for (const p of patterns.iframes) {
                if (src.includes(p)) {
                    if (isLargeVisible(iframe)) {
                        return {level: 'hard', reason: 'iframe_match: ' + p};
                    }
                    if (!soft) soft = {level: 'soft', reason: 'widget_present: ' + p};
                }
            }
        }

        // 3. 特征元素：同样按可见性+尺寸分级（.g-recaptcha 容器常为隐藏空 div）
        for (const sel of patterns.elements) {
            const nodes = Array.from(document.querySelectorAll(sel)).slice(0, 5);
            for (const node of nodes) {
                if (isLargeVisible(node)) {
                    return {level: 'hard', reason: 'element_match: ' + sel};
                }
                if (!soft) soft = {level: 'soft', reason: 'widget_present: ' + sel};
            }
        }

        // 4. 短文本挑战话术 + 强信号在场：hard（保持原有双信号防误报）
        const text = (document.body ? document.body.innerText : '').trim().toLowerCase();
        if (text.length > 0 && text.length < 500) {
            const hasWidget = soft !== null;
            if (hasWidget) {
                for (const p of patterns.short_texts) {
                    if (text.includes(p)) return {level: 'hard', reason: 'short_page_pattern: ' + p};
                }
            }
        }

        return soft;
    }
"""

DETECT_HUMAN_VERIFICATION_SCRIPT = """
(patterns) => {
""" + _ASSESS_CHALLENGE_JS + """
    return assessCaptcha(patterns);
}
"""

MUTATION_OBSERVER_SCRIPT = """
(patterns) => {
    // 去指纹设计：
    // - 不在 window 上留任何固定命名属性（防 Object.keys(window) 扫描），
    //   防重入标记用 Symbol.for（不可枚举，toString 也不暴露用途）；
    // - 回调函数名由 Python 侧每次 context 生成随机名注入（见 manager.py），
    //   不再用 onCaptchaDetected 这种可指纹的固定全局函数；
    // - 全部逻辑收敛在闭包内，不向全局作用域泄漏任何标识符。
    const GUARD = Symbol.for('@@c');
    if (window[GUARD]) return;
    window[GUARD] = true;

    // 回调名由 Python 侧每次生成（随机后缀），通过 init script 注入为同名全局函数。
    // 这里从 patterns 对象携带的字段读取，避免硬编码可指纹的固定名。
    const callbackName = patterns._cb;
    const report = (typeof callbackName === 'string' && typeof window[callbackName] === 'function')
        ? window[callbackName] : null;

""" + _ASSESS_CHALLENGE_JS + """
    let debounceTimer = null;
    let reportedHard = null;  // 已上报的 hard 原因（避免重复上报同一挑战）

    function checkCaptcha() {
        if (!patterns) return;
        // 分级评估：hard（大面积挑战）才上报人工接管；
        // soft（隐藏/小 widget 常驻）是正常防刷组件，不上报，防误报劫持 agent。
        const verdict = assessCaptcha(patterns);
        if (verdict && verdict.level === 'hard' && verdict.reason !== reportedHard) {
            reportedHard = verdict.reason;
            if (report) report(verdict.reason);
        }
    }

    // Initial check
    setTimeout(checkCaptcha, 1000);

    const observer = new MutationObserver((mutations) => {
        let hasSignificantChange = false;
        for (const m of mutations) {
            if (m.addedNodes.length > 0) {
                hasSignificantChange = true;
                break;
            }
        }

        if (hasSignificantChange) {
            if (debounceTimer) clearTimeout(debounceTimer);
            debounceTimer = setTimeout(checkCaptcha, 800);
        }
    });

    if (document.body) {
        observer.observe(document.body, { childList: true, subtree: true });
    } else {
        document.addEventListener('DOMContentLoaded', () => {
            observer.observe(document.body, { childList: true, subtree: true });
            checkCaptcha();
        });
    }
}
"""

GET_STATE_SCRIPT = """
(maxElements) => {
    let idCounter = 1;
    let visitedNodes = 0;
    const maxVisitedNodes = Math.max(maxElements * 20, 5000);
    const elements = [];
    let isTruncated = false;

    // 清除主文档及所有可见 iframe 文档中的旧标记
    document.querySelectorAll('[data-agent-id]').forEach(el => el.removeAttribute('data-agent-id'));
    document.querySelectorAll('iframe[data-agent-frame]').forEach(el => el.removeAttribute('data-agent-frame'));
    try {
        document.querySelectorAll('iframe').forEach(f => {
            if (f.contentDocument) {
                f.contentDocument.querySelectorAll('[data-agent-id]').forEach(el => el.removeAttribute('data-agent-id'));
            }
        });
    } catch (e) { /* 跨域 iframe 不可访问，忽略 */ }

    const winHeight = window.innerHeight;

    function classify(node, tagName) {
        const isEditable = tagName === 'input' || tagName === 'textarea' ||
                           node.isContentEditable ||
                           node.getAttribute('role') === 'textbox' ||
                           node.getAttribute('role') === 'combobox';

        const role = node.getAttribute('role');
        const tabIndex = node.getAttribute('tabindex');

        let isInteractive = isEditable || tagName === 'a' || tagName === 'button' || tagName === 'select' ||
                            role === 'button' || role === 'link' ||
                            role === 'menuitem' || role === 'option' ||
                            (node.hasAttribute('tabindex') && tabIndex !== '-1');

        if (!isInteractive) {
            const style = window.getComputedStyle(node);
            if (style.cursor === 'pointer' || style.cursor === 'text') isInteractive = true;
        }
        return { isEditable, isInteractive };
    }

    function extractText(node, isInteractive, isEditable) {
        let text = '';
        if (isInteractive) {
            if (isEditable) {
                text = (node.value || node.innerText || '').trim();
                if (!text) text = node.getAttribute('placeholder') || node.getAttribute('aria-label') || node.getAttribute('data-testid') || '';
            } else {
                text = node.getAttribute('aria-label') || node.getAttribute('alt') || node.innerText || node.value || node.getAttribute('data-testid') || '';
            }
        } else {
            // 非交互节点只取直属文本，避免父子俄罗斯套娃
            let directText = '';
            for (let child of node.childNodes) {
                if (child.nodeType === 3) directText += child.textContent;
            }
            directText = directText.trim();
            if (directText) {
                text = node.getAttribute('aria-label') || node.getAttribute('alt') || directText;
            }
        }
        text = text.replace(/\\n/g, ' ').replace(/\\s+/g, ' ').trim();
        if (text.length > 120) text = text.substring(0, 120) + '...';
        return text;
    }

    function checkVisible(node) {
        if (node.checkVisibility) {
            return node.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
        }
        const rect = node.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return false;
        const style = window.getComputedStyle(node);
        return !(style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0');
    }

    // 表单元素状态值（供 agent 感知当前值/勾选态/选中项）
    function readState(node, tagName) {
        try {
            if (tagName === 'input') {
                const type = (node.type || 'text').toLowerCase();
                if (type === 'checkbox' || type === 'radio') {
                    return node.checked ? 'checked' : 'unchecked';
                }
                const v = node.value || '';
                if (v) return v.slice(0, 60);
            } else if (tagName === 'select') {
                const opt = node.options[node.selectedIndex];
                if (opt) return (opt.text || opt.value || '').trim().slice(0, 60);
            }
        } catch (e) { /* 只读信息，失败不影响遍历 */ }
        return undefined;
    }

    // iframe 标记：每个 iframe 元素（含跨域）打上全局唯一 data-agent-frame 编号。
    // Python 侧用 page.frame_locator("iframe[data-agent-frame='N']").locator(...) 链式解析，
    // 与 frame 挂载顺序无关，嵌套 iframe 通过 framePath 链逐层穿透。
    let frameCounter = 0;

    function traverse(node, isInsideInteractive, framePath) {
        if (isTruncated) return;
        visitedNodes += 1;
        if (visitedNodes >= maxVisitedNodes || elements.length >= maxElements) {
            isTruncated = true;
            return;
        }

        if (node.nodeType !== 1) return; // Node.ELEMENT_NODE

        const tagName = node.tagName.toLowerCase();
        if (['script', 'style', 'noscript', 'meta', 'link', 'head'].includes(tagName)) return;

        const isVisible = checkVisible(node);
        if (!isVisible) return;

        const { isEditable, isInteractive } = classify(node, tagName);
        const text = extractText(node, isInteractive, isEditable);

        let shouldInclude = isInteractive || !!text;
        // 祖先去重：已在交互容器内的纯文本节点不重复输出
        if (shouldInclude && !isInteractive && isInsideInteractive) {
            shouldInclude = false;
        }

        if (shouldInclude) {
            const rect = node.getBoundingClientRect();
            let position = 'viewport';
            if (rect.bottom < 0) position = 'above';
            else if (rect.top > winHeight) position = 'below';

            const role = node.getAttribute('role') || tagName;
            node.setAttribute('data-agent-id', idCounter);
            elements.push({
                id: idCounter++,
                tag: tagName,
                role: role,
                text: text,
                type: node.type || undefined,
                isInteractive: isInteractive,
                position: position,
                // 主文档为空数组；每层 iframe 追加其 data-agent-frame 编号
                framePath: framePath,
                // 表单状态值：checkbox/radio 勾选态、select 选中项、input 当前值
                state: readState(node, tagName),
            });
        }

        const nextIsInsideInteractive = isInsideInteractive || isInteractive;

        // 1. Shadow DOM 穿透：open root 递归遍历其子树
        if (node.shadowRoot) {
            let child = node.shadowRoot.firstElementChild;
            while (child) {
                traverse(child, nextIsInsideInteractive, framePath);
                child = child.nextElementSibling;
            }
        }

        // 2. iframe：给 iframe 元素打编号（无论能否穿透内部），同源则递归进 contentDocument
        if (tagName === 'iframe') {
            frameCounter += 1;
            const fid = frameCounter;
            node.setAttribute('data-agent-frame', fid);
            const childPath = framePath.concat([fid]);
            try {
                const doc = node.contentDocument;
                if (doc && doc.body) {
                    traverseContainer(doc.body, nextIsInsideInteractive, childPath);
                }
            } catch (e) { /* 跨域 iframe 抛 SecurityError，编号保留但无法深入 */ }
            return; // iframe 自身不再遍历普通子节点（无意义）
        }

        // 3. 普通子节点
        let child = node.firstElementChild;
        while (child) {
            traverse(child, nextIsInsideInteractive, framePath);
            child = child.nextElementSibling;
        }
    }

    function traverseContainer(container, isInsideInteractive, framePath) {
        for (const child of Array.from(container.children)) {
            traverse(child, isInsideInteractive, framePath);
        }
    }

    if (document.body) {
        traverseContainer(document.body, false, []);
    }

    return {
        url: document.location.href,
        title: document.title,
        elements: elements,
        truncated: isTruncated,
        scrollY: Math.round(window.scrollY),
        scrollHeight: Math.round(document.body ? document.body.scrollHeight : 0),
        clientHeight: Math.round(window.innerHeight),
    };
}
"""

EXTRACT_CONTENT_SCRIPT = r"""
(args) => {
    const maxChars = Math.max(1000, Math.min(args && args.maxChars || 60000, 120000));
    const includeLinks = !args || args.includeLinks !== false;
    const maxLinks = Math.max(0, Math.min(args && args.maxLinks || 80, 200));

    function textOf(node, preserveStructure = false) {
        const raw = node && (node.innerText || node.textContent) || '';
        if (preserveStructure) {
            return raw.replace(/[ \t\f\v]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
        }
        return raw.replace(/\s+/g, ' ').trim();
    }

    function meta(selector, attr = 'content') {
        const node = document.querySelector(selector);
        return node ? (node.getAttribute(attr) || '').trim() : '';
    }

    function cleanClone(node) {
        const clone = node.cloneNode(true);
        clone.querySelectorAll('script, style, noscript, svg, canvas, nav, header, footer, aside').forEach(el => el.remove());
        return clone;
    }

    const selectors = ['article', 'main', '[role="main"]', '.article', '.post', '.entry-content', '.content', '#content', '.markdown-body', '.document', '.docs-content'];
    const candidates = [];
    for (const selector of selectors) {
        for (const node of Array.from(document.querySelectorAll(selector)).slice(0, 8)) {
            const cleaned = cleanClone(node);
            const text = textOf(cleaned, true);
            if (text.length > 100) {
                const linkText = Array.from(cleaned.querySelectorAll('a')).map(a => textOf(a)).join(' ');
                const linkDensity = text.length ? linkText.length / text.length : 0;
                const paragraphs = cleaned.querySelectorAll('p, li, pre, blockquote').length;
                candidates.push({selector, text, score: text.length + paragraphs * 80 - linkDensity * 1000});
            }
        }
    }
    if (document.body) {
        const cleaned = cleanClone(document.body);
        const text = textOf(cleaned, true);
        candidates.push({selector: 'body', text, score: text.length * 0.6});
    }
    candidates.sort((a, b) => b.score - a.score);
    const best = candidates[0] || {selector: 'none', text: '', score: 0};
    const fullText = best.text || '';
    const truncated = fullText.length > maxChars;
    const contentText = truncated ? fullText.slice(0, maxChars) : fullText;

    const headings = Array.from(document.querySelectorAll('h1, h2, h3')).slice(0, 80).map(h => ({
        level: h.tagName.toLowerCase(),
        text: textOf(h).slice(0, 300),
    })).filter(h => h.text);

    let links = [];
    if (includeLinks) {
        links = Array.from(document.querySelectorAll('a[href]')).slice(0, 400).map(a => ({
            text: textOf(a).slice(0, 160),
            href: a.href,
            rel: a.getAttribute('rel') || '',
            target: a.getAttribute('target') || '',
            isExternal: a.hostname && a.hostname !== location.hostname,
        })).filter(link => link.href).slice(0, maxLinks);
    }

    return {
        url: document.location.href,
        title: document.title || '',
        readyState: document.readyState,
        lang: document.documentElement ? (document.documentElement.lang || '') : '',
        metadata: {
            description: meta('meta[name="description"]'),
            canonical: meta('link[rel="canonical"]', 'href'),
            author: meta('meta[name="author"]'),
            publishedTime: meta('meta[property="article:published_time"]') || meta('meta[name="pubdate"]') || meta('time[datetime]', 'datetime'),
            modifiedTime: meta('meta[property="article:modified_time"]'),
            ogTitle: meta('meta[property="og:title"]'),
            ogDescription: meta('meta[property="og:description"]'),
            ogSiteName: meta('meta[property="og:site_name"]'),
        },
        headings,
        links,
        content: {
            sourceSelector: best.selector,
            text: contentText,
            textLength: fullText.length,
            truncated,
        },
        pageState: {
            scrollY: Math.round(window.scrollY),
            scrollHeight: Math.round(document.body ? document.body.scrollHeight : 0),
            clientHeight: Math.round(window.innerHeight),
        },
    };
}
"""

SCROLL_INTO_CONTAINER_SCRIPT = """
({ selector, direction, amount }) => {
    const el = document.querySelector(selector);
    if (!el) return false;

    let container = el;
    while (container && container !== document.body && container !== document.documentElement) {
        if (container.scrollHeight > container.clientHeight) {
            const style = window.getComputedStyle(container);
            if (style.overflowY === 'auto' || style.overflowY === 'scroll') {
                break;
            }
        }
        container = container.parentElement;
    }

    if (!container || container === document.body || container === document.documentElement) {
        container = window;
    }

    if (direction === 'top') {
        container.scrollTo(0, 0);
    } else if (direction === 'bottom') {
        if (container === window) {
            container.scrollTo(0, document.body.scrollHeight);
        } else {
            container.scrollTo(0, container.scrollHeight);
        }
    } else if (direction === 'down') {
        container.scrollBy(0, amount);
    } else {
        container.scrollBy(0, -amount);
    }
    return true;
}
"""

WAIT_FOR_SCRIPT = """
async (args) => {
    const needle = (args && args.text || '').toLowerCase();
    const timeoutMs = (args && args.timeoutMs) || 10000;
    const startTime = performance.now();

    function containsText() {
        // body.innerText 会触发 reflow，用 textContent 近似匹配可接受（大小写不敏感）
        const text = (document.body ? document.body.innerText : '').toLowerCase();
        return text.includes(needle);
    }

    if (containsText()) return true;

    return await new Promise(resolve => {
        let timer = null;
        const check = () => {
            if (containsText()) {
                if (timer) clearTimeout(timer);
                observer.disconnect();
                resolve(true);
            }
        };
        const observer = new MutationObserver(() => {
            if (timer) clearTimeout(timer);
            // DOM 变化后 100ms 静默再检查，避免高频 reflow
            timer = setTimeout(check, 100);
        });
        observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
        setTimeout(() => {
            observer.disconnect();
            if (timer) clearTimeout(timer);
            resolve(containsText());
        }, timeoutMs);
    });
}
"""

SELECT_OPTIONS_SCRIPT = """
(selector) => {
    const el = document.querySelector(selector);
    if (!el || el.tagName.toLowerCase() !== 'select') return null;
    return Array.from(el.options).map(o => ({value: o.value, text: (o.text || '').trim()}));
}
"""

# 操作完成后清除元素标记（降低自动化指纹可检测性）。
# 供 close()/页面级清理使用：把 data-agent-id 与 data-agent-frame 一并移除。
CLEAR_AGENT_MARKS_SCRIPT = """
() => {
    const clean = (doc) => {
        doc.querySelectorAll('[data-agent-id]').forEach(el => el.removeAttribute('data-agent-id'));
        doc.querySelectorAll('iframe[data-agent-frame]').forEach(el => el.removeAttribute('data-agent-frame'));
    };
    clean(document);
    try {
        document.querySelectorAll('iframe').forEach(f => {
            if (f.contentDocument) clean(f.contentDocument);
        });
    } catch (e) { /* 跨域忽略 */ }
    return true;
}
"""

# 页面轻量快照：click 前后各取一次，对比得出变化摘要。
# 刻意不用整页 HTML hash（太重），只取与 agent 决策相关的信号。
PAGE_SNAPSHOT_SCRIPT = """
() => {
    let visible = 0;
    try {
        document.querySelectorAll('a, button, input, select, textarea, [role="button"]').forEach(el => {
            if (el.getClientRects().length > 0) visible += 1;
        });
    } catch (e) { /* 忽略 */ }
    return {
        url: document.location.href,
        title: (document.title || '').slice(0, 120),
        // 只统计可见交互元素（display:none 展开后 delta 才能体现变化）
        interactive: visible,
        bodyChars: document.body ? document.body.innerText.length : 0,
    };
}
"""

# 等待 DOM 稳定：MutationObserver 观察到 quiet_ms 毫秒无变更即认为收敛。
# 上限 timeout_ms，超时返回 false（调用方决定是否容忍）。
DOM_QUIESCE_SCRIPT = """
(args) => new Promise(resolve => {
    const quietMs = (args && args.quietMs) || 400;
    const timeoutMs = (args && args.timeoutMs) || 3000;
    const started = performance.now();

    let timer = null;
    const settle = () => resolve(true);
    const arm = () => {
        if (performance.now() - started >= timeoutMs) {
            observer.disconnect();
            resolve(false);
            return;
        }
        if (timer) clearTimeout(timer);
        timer = setTimeout(() => {
            observer.disconnect();
            settle();
        }, quietMs);
    };

    const observer = new MutationObserver(arm);
    observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
    arm();
})
"""

# 贝塞尔鼠标轨迹：用 CDP Input.dispatchMouseEvent 逐点派发（trusted 事件，
# 坐标含随机扰动与缓动，模拟人类手部运动）。由 Python 侧计算轨迹点后调用。
# 此脚本负责把元素中心坐标 + 视口信息回传给 Python 做轨迹规划。
ELEMENT_CENTER_SCRIPT = """
(selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    // 中心点加微小随机偏移（人不会每次都点正中心）
    const jitterX = (Math.random() - 0.5) * Math.min(r.width * 0.3, 12);
    const jitterY = (Math.random() - 0.5) * Math.min(r.height * 0.3, 8);
    return {
        x: Math.round(r.left + r.width / 2 + jitterX),
        y: Math.round(r.top + r.height / 2 + jitterY),
        inViewport: r.top >= 0 && r.left >= 0 && r.bottom <= window.innerHeight && r.right <= window.innerWidth,
        viewportW: window.innerWidth,
        viewportH: window.innerHeight,
    };
}
"""

# 检测元素是否属于登录/敏感表单（自动切换逐字 trusted 输入）
SENSITIVE_FORM_DETECT_SCRIPT = """
(selector) => {
    const el = document.querySelector(selector);
    if (!el) return false;
    const tag = el.tagName.toLowerCase();
    // 密码框必逐字
    if (tag === 'input' && (el.type || '').toLowerCase() === 'password') return true;
    // 沿祖先找 form / 登录容器特征
    let node = el;
    for (let i = 0; i < 6 && node && node !== document.body; i++) {
        const sig = ((node.id || '') + ' ' + (typeof node.className === 'string' ? node.className : '') + ' ' + (node.getAttribute('name') || '')).toLowerCase();
        if (/login|signin|sign-in|password|auth|credential|session|passwd/.test(sig)) return true;
        const role = node.getAttribute && node.getAttribute('role');
        if (role === 'form') {
            const fa = (node.action || '').toLowerCase();
            if (/login|signin|auth|session|password/.test(fa)) return true;
        }
        node = node.parentElement;
    }
    // autocomplete 提示也是强信号
    const ac = (el.getAttribute && el.getAttribute('autocomplete') || '').toLowerCase();
    return ac.includes('password') || ac.includes('username') || ac.includes('email');
}
"""

# 表单元素状态值（用于 description 校验和 get_state 的状态展示）
ELEMENT_STATE_SCRIPT = """
(selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    const tag = el.tagName.toLowerCase();
    const state = {};
    if (tag === 'input') {
        const type = (el.type || 'text').toLowerCase();
        if (type === 'checkbox' || type === 'radio') state.checked = !!el.checked;
        else state.value = (el.value || '').slice(0, 100);
    } else if (tag === 'select') {
        const opt = el.options[el.selectedIndex];
        state.value = opt ? opt.value : '';
        state.text = opt ? (opt.text || '').trim() : '';
    } else if (tag === 'textarea') {
        state.value = (el.value || '').slice(0, 100);
    }
    return state;
}
"""
