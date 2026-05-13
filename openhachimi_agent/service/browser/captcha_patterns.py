"""CAPTCHA detection patterns."""

CAPTCHA_PATTERNS = {
    "titles": [
        "just a moment...",
        "attention required! | cloudflare",
        "verify you are human",
        "checking your browser",
    ],
    "short_texts": [
        "checking your browser before accessing",
        "verify you are human",
        "please stand by, while we are checking your browser",
        "cf-challenge",
        "拖动滑块完成拼图",
        "请完成安全验证",
        "点击按钮进行验证",
        "请拖动滑块",
        "安全检查中",
        "访问过于频繁",
    ],
    "iframes": [
        "challenges.cloudflare.com",
        "newassets.hcaptcha.com",
        "recaptcha",
        "arkoselabs.com",
        "funcaptcha.com",
    ],
    "elements": [
        "#cf-challenge-error-title",
        ".cf-turnstile",
        ".g-recaptcha",
        ".geetest_panel",
        ".yidun_popup",
        "#px-captcha",
        ".fc-iframe-wrap",
    ]
}
