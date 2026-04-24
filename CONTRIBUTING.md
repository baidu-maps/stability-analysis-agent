# 参与贡献（Contributing）

感谢你对 **Stability Analysis Agent** 的关注。参与前请先阅读本文，尤其是 **DCO** 与 **许可证** 部分。

## 许可证

本仓库在 **[Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0)** 下发布，完整条文见根目录 [`LICENSE`](./LICENSE)。分发与再许可时请遵守 Apache-2.0 及根目录 [`NOTICE`](./NOTICE) 中的说明。

## 贡献协议：DCO（Developer Certificate of Origin）

本项目采用 **DCO**，**不使用**单独的贡献者许可协议（CLA）签署流程。

你向本仓库提交的每一次贡献（包括但不限于 Pull Request、通过 Issue 等方式被采纳的补丁），均表示你同意 **Developer Certificate of Origin 1.1**（见下文「DCO 1.1 全文」）的条款。

### 如何在提交中体现 DCO

每个提交信息末尾需包含 **`Signed-off-by`** 行，格式如下（姓名与邮箱须与你在 Git 中的身份一致，且你有权代表该身份作出声明）：

```text
Signed-off-by: Random J Developer <random@developer.example.org>
```

推荐使用 Git 的 `-s` / `--signoff` 自动附加该行：

```bash
git commit -s -m "fix: describe your change"
```

若一次 Pull Request 包含多个提交，**每个提交**都应带有有效的 `Signed-off-by`。

### 未签署（no sign-off）的提交

未带 `Signed-off-by` 的提交可能无法合并；维护者会请你使用 `git commit --amend -s` 或 `git rebase` 补全后再次推送。

### 代理与雇主贡献

若你的贡献属于工作职责，请确保你已获得雇主或权利方授权，并在 `Signed-off-by` 中使用你有权绑定的身份。

---

## Developer Certificate of Origin（DCO 1.1 全文）

以下为 **Developer Certificate of Origin Version 1.1** 的原文（与 [developercertificate.org](https://developercertificate.org/) 一致），以英文为准。

```text
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

---

## 工作流程建议

1. 从默认分支创建功能分支。
2. 尽量保持提交原子化、说明清楚；遵守团队已有的代码风格与测试习惯。
3. 提交前本地运行相关测试（见根目录 [`README.md`](./README.md)）。
4. 打开 Pull Request，在描述中说明动机、行为变更与风险；若有相关 Issue 请关联。

## 行为与安全

- 请保持尊重、专业的沟通。
- **安全漏洞**请勿通过公开 Issue 讨论，请见 [`SECURITY.md`](./SECURITY.md)。
