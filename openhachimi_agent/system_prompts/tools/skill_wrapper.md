<skill name="{{ skill_name }}" skill_root="{{ skill_root }}" path="{{ skill_path }}">
{{ intro }}[Skill Metadata]
- skill_path: {{ skill_path }}
- skill_root: {{ skill_root }}

[Path Note]
read_file、list_files、find_files 和 search_text 的相对路径仍相对于当前项目工作区根目录，不会自动相对于本 skill 目录解析。
如果本 skill 需要读取自身附带的参考文件、模板、示例或脚本，请将 skill 文档中的相对路径与上方 skill_root 拼接成绝对路径后再调用文件工具。

{{ content }}
</skill>