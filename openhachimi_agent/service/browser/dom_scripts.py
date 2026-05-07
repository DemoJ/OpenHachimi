"""JavaScript scripts for browser DOM manipulation and analysis."""

DETECT_HUMAN_VERIFICATION_SCRIPT = """
() => {
    const title = (document.title || '').toLowerCase();
    const titlePatterns = [
        'just a moment...',
        'attention required! | cloudflare',
        'verify you are human'
    ];
    if (titlePatterns.includes(title) || title.startsWith('checking your browser')) {
        return 'title_match: ' + title;
    }

    const iframes = Array.from(document.querySelectorAll('iframe'));
    for (const iframe of iframes) {
        const src = (iframe.src || '').toLowerCase();
        if (src.includes('challenges.cloudflare.com') || 
            src.includes('newassets.hcaptcha.com')) {
            return 'iframe_match: ' + src.substring(0, 60);
        }
    }

    if (document.querySelector('#cf-challenge-error-title') || document.querySelector('.cf-turnstile')) {
        return 'cf_challenge_element';
    }
    
    const text = (document.body ? document.body.innerText : '').trim().toLowerCase();
    if (text.length > 0 && text.length < 500) {
        const shortPatterns = [
            'checking your browser before accessing',
            'verify you are human',
            'please stand by, while we are checking your browser',
            'cf-challenge',
            '拖动滑块完成拼图',
            '请完成安全验证'
        ];
        for (const p of shortPatterns) {
            if (text.includes(p)) return 'short_page_pattern: ' + p;
        }
    }

    const elements = Array.from(document.querySelectorAll('[id],[class]')).slice(0, 200);
    for (const el of elements) {
        const id = (el.id || '').toLowerCase();
        const cls = (el.className || '');
        const clsStr = (typeof cls === 'string' ? cls : '').toLowerCase();
        if (id === 'px-captcha' || clsStr.includes('geetest_panel') || clsStr.includes('yidun_popup')) {
            return 'captcha_overlay: ' + (id || clsStr);
        }
    }
    
    return null;
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
