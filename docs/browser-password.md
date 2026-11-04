# 访问密码与恢复

网页访问密码属于本项目，不是原 V360 Pro 账号密码、摄像头设备密码或厂商接口参数。它与程序维护用的 API 令牌分别管理。

## 设置与登录

历史录像 → 管理与诊断 → 修改访问密码。新密码支持汉字、字母、数字及空格，长度为 8–128 个 Unicode 字符，不自动去掉首尾空格。需要当前密码校验；修改后旧浏览器登录失效，其他设备需要重新登录。

初次尚未设置独立密码时，可用私有 `web.json` 中的原 API 令牌登录；设置后网页使用新密码，API Bearer 令牌保留供维护和恢复。浏览器用 JSON 提交密码，不把汉字放到 HTTP 请求头中。

服务端只保存随机盐和 PBKDF2-HMAC-SHA256（210000 轮）的校验值，不保存独立密码明文。登录尝试有限速，浏览器使用独立、可撤销的 HttpOnly 会话 Cookie；生产使用 HTTPS 和 Secure，详见 [登录保持](browser-sessions-responsive.md)。

记住登录为固定十五天；不勾选时服务端最长十二小时。升级前发出的七天会话不自动延长，重新登录并勾选后才获得十五天。

## 中文输入的兼容与边界

部分系统的原生密码框会强制使用不能输入中文的安全键盘。当前四个输入字段从初始就是普通文本类型，用 `-webkit-text-security` 遮盖字符，小眼睛仅控制是否显示，无需先点击“中文模式”。退出、关闭管理窗口及离开页面会恢复遮盖；浏览器不支持遮盖时明确提示文字可见。

圆点只防旁人看到，不等于系统安全键盘。普通输入法仍能接触输入内容，也可能学习或同步；应使用可信输入法和本站专用长口令。网页没有标准方法强制厂商安全键盘增加中文；`inputmode` 只是提示，不能作安全保证。

后续可考虑原生密码框配合可信密码管理器自动填充，或使用指纹/设备解锁的通行密钥避免输入；都需要验证实际设备。通行密钥还需准备稳定域名和 HTTPS，目前未实现。此次中文兼容不能被描述为所有手机均已验收。

参考：[密码框](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/input/password)、[遮盖属性](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/-webkit-text-security)、[输入提示](https://html.spec.whatwg.org/multipage/interaction.html#input-modalities:-the-inputmode-attribute)、[WebAuthn](https://www.w3.org/TR/webauthn-3/)。

## 令牌保存和忘记密码

本地 API 令牌位于 `.private/web.json`，部署后在 `/srv/v360-web/config/web.json`。私有 JSON 权限为 600，升级的元数据快照包含配置和数据库。可另存可信密码管理器，不放进 Git、截图或聊天。

拥有服务器管理权限时可以从配置恢复 API 令牌，并用维护权限恢复网页访问；原令牌不是丢失即永久失去摄像头的硬件密钥。独立密码只有校验值，不能从数据库还原明文。不要为忘记网页密码重置远方摄像头，也不要删除数据库或录像来尝试恢复。

安全相关检查位于 `tests/test_web_password.py`、`tests/test_browser_sessions.py`、`tests/browser_password.cjs` 和 `tests/password_visibility.cjs`；这些不证明具体输入法不记录内容。
