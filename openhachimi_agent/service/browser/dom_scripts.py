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
    let isTruncated = false;
    
    const winHeight = window.innerHeight;
    
    function traverse(node, isInsideInteractive) {
        if (isTruncated) return;
        if (elements.length >= maxElements) {
            isTruncated = true;
            return;
        }
        
        // 1. 过滤文本节点等非元素节点
        if (node.nodeType !== 1) return; // Node.ELEMENT_NODE
        
        // 2. 过滤无意义标签
        const tagName = node.tagName.toLowerCase();
        if (['script', 'style', 'noscript', 'meta', 'link', 'head'].includes(tagName)) return;
        
        // 3. 剪枝：跳过不可见元素树，极大地提升性能
        let isVisible = true;
        if (node.checkVisibility) {
            isVisible = node.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
        } else {
            const rect = node.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) {
                isVisible = false;
            } else {
                const style = window.getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                    isVisible = false;
                }
            }
        }
        if (!isVisible) return;
        
        const rect = node.getBoundingClientRect();
        
        // 4. 计算元素相对视口的位置
        let position = 'viewport';
        if (rect.bottom < 0) position = 'above';
        else if (rect.top > winHeight) position = 'below';
        
        // 5. 交互性检测
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
        
        // 6. 移除 elementFromPoint 检测以避免 Layout Thrashing
        
        // 7. 文本提取
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
                if (child.nodeType === 3) directText += child.textContent; // Node.TEXT_NODE
            }
            directText = directText.trim();
            if (directText) {
                text = node.getAttribute('aria-label') || node.getAttribute('alt') || directText;
            }
        }
        
        text = text.replace(/\\n/g, ' ').replace(/\\s+/g, ' ').trim();
        if (text.length > 120) text = text.substring(0, 120) + '...';
        
        let shouldInclude = isInteractive || text;
        
        // 8. 祖先去重优化 (DFS 向下传递状态)
        if (shouldInclude && !isInteractive && isInsideInteractive) {
            // 已在交互容器内，忽略非交互的纯文本节点，将其文本视作属于父容器
            shouldInclude = false;
        }
        
        if (shouldInclude) {
            const elData = {
                id: idCounter++,
                tag: tagName,
                role: role || tagName,
                text: text,
                type: node.type || undefined,
                isInteractive: isInteractive,
                position: position,
            };
            
            node.setAttribute('data-agent-id', elData.id);
            elements.push(elData);
        }
        
        const nextIsInsideInteractive = isInsideInteractive || isInteractive;
        
        // 递归遍历子节点
        let child = node.firstElementChild;
        while (child) {
            traverse(child, nextIsInsideInteractive);
            child = child.nextElementSibling;
        }
    }
    
    if (document.body) {
        traverse(document.body, false);
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
