import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'

const md: MarkdownIt = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: true,
  highlight(str: string, lang: string): string {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return `<pre class="hljs"><code>${hljs.highlight(str, { language: lang, ignoreIllegals: true }).value}</code></pre>`
      } catch { /* fallthrough */ }
    }
    return `<pre class="hljs"><code>${md.utils.escapeHtml(str)}</code></pre>`
  },
})
// 显式启用表格规则（markdown-it 默认已启用，这里确保不被意外禁用）。
md.enable(['table'])

export function renderMarkdown(text: string): string {
  return md.render(text)
}

export function renderStreaming(text: string): string {
  return md.render(text)
}

export function highlightAll(): void {
  document.querySelectorAll('.hljs code').forEach((block) => {
    hljs.highlightElement(block as HTMLElement)
  })
}