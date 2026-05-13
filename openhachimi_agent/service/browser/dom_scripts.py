"""JavaScript scripts for browser DOM manipulation and analysis."""

DETECT_HUMAN_VERIFICATION_SCRIPT = """
(patterns) => {
    if (!patterns) return null;
    
    // Check titles
    const title = (document.title || '').toLowerCase();
    for (const p of patterns.titles) {
        if (title.includes(p)) return 'title_match: ' + p;
    }

    // Check iframes
    const iframes = Array.from(document.querySelectorAll('iframe'));
    for (const iframe of iframes) {
        const src = (iframe.src || '').toLowerCase();
        for (const p of patterns.iframes) {
            if (src.includes(p)) return 'iframe_match: ' + p;
        }
    }

    // Check elements
    for (const sel of patterns.elements) {
        if (document.querySelector(sel)) return 'element_match: ' + sel;
    }
    
    // Check short body text
    const text = (document.body ? document.body.innerText : '').trim().toLowerCase();
    if (text.length > 0 && text.length < 500) {
        for (const p of patterns.short_texts) {
            if (text.includes(p)) return 'short_page_pattern: ' + p;
        }
    }

    return null;
}
"""

MUTATION_OBSERVER_SCRIPT = """
(patterns) => {
    if (window._hachimiCaptchaObserverInjected) return;
    window._hachimiCaptchaObserverInjected = true;
    
    let debounceTimer = null;
    
    function checkCaptcha() {
        if (!patterns) return;
        
        // Quick element check
        for (const sel of patterns.elements) {
            if (document.querySelector(sel)) {
                if (window.onCaptchaDetected) window.onCaptchaDetected('element_match: ' + sel);
                return;
            }
        }
        
        // Quick iframe check
        const iframes = Array.from(document.querySelectorAll('iframe'));
        for (const iframe of iframes) {
            const src = (iframe.src || '').toLowerCase();
            for (const p of patterns.iframes) {
                if (src.includes(p)) {
                    if (window.onCaptchaDetected) window.onCaptchaDetected('iframe_match: ' + p);
                    return;
                }
            }
        }
        
        // Quick text check (only if short)
        const text = (document.body ? document.body.innerText : '').trim().toLowerCase();
        if (text.length > 0 && text.length < 500) {
            for (const p of patterns.short_texts) {
                if (text.includes(p)) {
                    if (window.onCaptchaDetected) window.onCaptchaDetected('short_page_pattern: ' + p);
                    return;
                }
            }
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
    const elements = [];
    const interactiveNodes = [];
    
    const nodes = document.querySelectorAll('*');
    const winHeight = window.innerHeight;
    const winWidth  = window.innerWidth;
    
    for (const node of nodes) {
        if (elements.length >= maxElements) break;
        
        // 1. 过滤无意义标签
        const tagName = node.tagName.toLowerCase();
        if (['script','style','noscript','meta','link','head'].includes(tagName)) continue;
        
        // 2. 尺寸过滤：零尺寸 = 未渲染，直接跳过
        const rect = node.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        
        // 3. CSS 可见性过滤
        const style = window.getComputedStyle(node);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
        
        // 4. 计算元素相对视口的位置（用于输出标记，不用于过滤）
        let position;
        if (rect.bottom < 0)       position = 'above';    // 视口上方
        else if (rect.top > winHeight) position = 'below'; // 视口下方
        else                           position = 'viewport'; // 当前可见
        
        // 5. 交互性检测
        const isEditable = tagName === 'input' || tagName === 'textarea' ||
                           node.isContentEditable ||
                           node.getAttribute('role') === 'textbox' ||
                           node.getAttribute('role') === 'combobox';
                           
        let isInteractive = isEditable || tagName === 'a' || tagName === 'button' || tagName === 'select' ||
                            node.getAttribute('role') === 'button' || node.getAttribute('role') === 'link' ||
                            node.getAttribute('role') === 'menuitem' || node.getAttribute('role') === 'option' ||
                            (node.hasAttribute('tabindex') && node.getAttribute('tabindex') !== '-1') ||
                            style.cursor === 'pointer' || style.cursor === 'text';
        
        // 6. 物理遮挡剔除（仅对视口内元素有意义，视口外无浮层覆盖问题）
        if (isInteractive && position === 'viewport') {
            const centerX = rect.left + rect.width / 2;
            const centerY = rect.top  + rect.height / 2;
            if (centerX >= 0 && centerX <= winWidth && centerY >= 0 && centerY <= winHeight) {
                const topEl = document.elementFromPoint(centerX, centerY);
                if (topEl && topEl !== node && !node.contains(topEl) && !topEl.contains(node)) {
                    let p1 = node, common = null, depth = 0;
                    while (p1 && depth < 5) {
                        if (p1.contains(topEl)) { common = p1; break; }
                        p1 = p1.parentElement;
                        depth++;
                    }
                    if (!common) isInteractive = false;
                }
            }
        }
        
        // 7. 文本提取
        let text = '';
        if (isInteractive) {
            if (isEditable) {
                let val = (node.value || node.innerText || '').trim();
                if (!val) val = node.getAttribute('placeholder') || node.getAttribute('aria-label') || node.getAttribute('data-testid') || '';
                text = val;
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
            } else {
                continue;
            }
        }
        
        text = text.replace(/\\n/g, ' ').replace(/\\s+/g, ' ').trim();
        if (text.length > 120) text = text.substring(0, 120) + '...';
        if (!text && !isInteractive) continue;
        
        // 8. 祖先去重
        if (!isInteractive) {
            if (interactiveNodes.some(parent => parent.contains(node))) continue;
        }
        
        const role = node.getAttribute('role') || tagName;
        const elData = {
            id: idCounter++,
            tag: tagName,
            role: role,
            text: text,
            type: node.type || undefined,
            isInteractive: isInteractive,
            position: position,
        };
        
        node.setAttribute('data-agent-id', elData.id);
        elements.push(elData);
        if (isInteractive) interactiveNodes.push(node);
    }
    
    return {
        url: document.location.href,
        title: document.title,
        elements: elements,
        truncated: elements.length >= maxElements,
        scrollY: Math.round(window.scrollY),
        scrollHeight: Math.round(document.body.scrollHeight),
        clientHeight: Math.round(window.innerHeight),
    };
}
"""
