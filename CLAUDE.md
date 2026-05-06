# AionsHome 项目规范

## Shell 工具

**始终使用 PowerShell tool，不用 Bash tool。**
项目在 Windows 上，Bash tool 不认 `D:\` 路径会报错。

## Commit 格式

```
<type>: <中文简述>
```

type 用英文（feat / fix / docs / chore / refactor），简述用中文，不加多余前缀。

## Java 文件

打包 AionApp 前检查并移除 Java 文件的 UTF-8 BOM，否则编译失败。

## 分支合并

feature 分支合并到 main 前先 squash，保持提交历史整洁。
