# 将来要给插件接 AI 时的备查资料

**这份文档不描述现有行为**——本插件到目前为止**一次 LLM 都没调用过**：
`requirements.txt` 只有 `aiosqlite`，全部文案是本地静态池 + `random.choice`
（`faith_messages.py` / `prayer_messages.py` / `wager_messages.py` / `wish_messages.py`），
README 也明确承诺「指令回复不经过 AI 处理，固定格式输出」。

写在这里是因为「怎么接」这件事的答案很具体、很容易记错，而调研成本不低。
内容取自两个已上线插件的实现（`astrbot_plugin_faq`、`astrbot_plugin_keyword_reply`）。

---

## 1. 调用形状（AstrBot v4）

不要自己去读配置里的 API key 或直接 new 一个 provider，走 `Context` 提供的两步：

```python
# 第一步：拿到本会话该用的 provider id
provider_id = cfg_get(config, "llm_provider_id")          # 允许诸神手动指定
if not provider_id:
    provider_id = await self.context.get_current_chat_provider_id(
        umo=event.unified_msg_origin                            # 用完整会话串，不要拼
    )
if not provider_id:
    return None            # 没配 LLM：静默跳过，绝不影响非 AI 功能（这是硬要求）

# 第二步：生成
resp = await self.context.llm_generate(
    chat_provider_id=provider_id,
    prompt=user_text,                     # 用户这句话
    system_prompt=system_prompt,          # 组装好的模板（见下）
)
text = getattr(resp, "completion_text", "") or ""
return text.strip()[:max_chars] or None    # 截断 + 空串当失败
```

要点：

- `get_current_chat_provider_id(umo=...)` 拿的是**当前会话**用的 provider，
  所以用户在 WebUI 换模型后立即生效，不需要插件自己存一份。
- `umo` 必须用 `event.unified_msg_origin` 原值。本插件已经踩过"手工拼会话串"
  的坑（v4 要 `<平台>:GroupMessage:<群号>` 三段，拼错会抛
  `不合法的 session 字符串`，消息被直接丢弃）——见 CHANGELOG 里赌局开奖那两条。
- `llm_generate` 整段包 try/except，失败只记日志并返回 `None`。
  **AI 是加分项，不能让它把主流程（落分、发消息）带崩。**

## 2. 配置项怎么写

在 `_conf_schema.json` 里加一个 `_special`，WebUI 会把它渲染成**提供商下拉框**，
省掉让诸神手抄 provider id：

```json
"llm_provider_id": {
  "description": "AI 兜底使用的 LLM 提供商",
  "type": "string",
  "default": "",
  "_special": "select_provider"
}
```

配合一个总开关（`enable_llm_fallback`）和一个「留空 = 用当前会话默认」的约定。
schema 是默认值的唯一事实来源（见 `tests/test_config_access.py`），
别在代码里再写一份。

## 3. token 节制四件套

这部分是调研里最值得抄的——两个插件都用了同一套组合，缺一个就会烧 token：

1. **仅 @ 机器人时触发**（`ai_only_when_at_bot`）。默认开。
   判断方式：遍历消息段，找 `At` 组件里 `qq == event.get_self_id()`。
2. **像问题的消息才外发**。长度下限 + 问句标记正则，闲聊直接不发。
   参考实现（`astrbot_plugin_faq` 的 `looks_like_question`）：

   ```python
   QUESTION_HINT_RE = re.compile(
       r"[?？]$|[?？]|吗|呢|么|怎|如何|多少|什么|为啥|为什么|哪|谁|咋|几|能不能|可以不可以"
   )
   # 长度门槛默认 6 字
   return len(text) >= min_chars and bool(QUESTION_HINT_RE.search(text))
   ```

3. **上下文按字符预算截断**，而不是按条数。逐条累加，超过预算就停：

   ```python
   MAX_ENTRIES, MAX_CHARS = 40, 3000
   lines, total = [], 0
   for entry in entries[:MAX_ENTRIES]:
       line = f"【Q】{entry.question}｜【A】{entry.answer}"
       total += len(line)
       if total > MAX_CHARS:
           break
       lines.append(line)
   knowledge = "\n".join(lines) or "（暂无）"
   ```

4. **按会话冷却**（不是按用户）。同一个群短时间内反复问，只答一次。

## 4. 系统提示词模板

模板放在配置里、支持占位符，诸神可以自己改。约定用 `{faq}` / `{max_chars}`
这类占位符，**替换时请走 `text_utils.render_template`**——模板是用户可编辑文本，
里面出现落单的 `{` 或 JSON 示例都很正常，`str.format` 会直接抛
`ValueError: Single '{' encountered`（详见该函数的 docstring 与
`tests/test_template_render.py`）。

## 5. 接之前要先决定的事

- **README 的承诺**：「指令回复不经过 AI 处理，固定格式输出」是现在写死的一句话。
  引入 AI 后要么改掉它，要么把 AI 严格限制在新增的、明确标注的路径上
  （例如只在「未命中任何指令」时兜底），不要让指令回复变得不确定。
- **`event.stop_event()` 的时机**：命中知识库/兜底成功并回复后要 stop_event，
  否则宿主默认 LLM 管线还会对同一条消息再答一次，群友看到两条。
  本插件在祷词路径上已经在用这个机制（`commands/prayer.py`）。
- **`metadata.yaml` 的 `astrbot_version`**：如果用到的新 API 需要更高的宿主版本
  （例如插件 WebUI 面板需要 v4.24.1+），下限要跟着抬，别只在代码里 try/except。

## 6. 参考实现

| 仓库 | 可参考的部分 |
|---|---|
| `astrbot_plugin_faq` | `Context.llm_generate` 的调用形状、prompt 组装与字符预算截断、问句门控、`_special: "select_provider"` |
| `astrbot_plugin_keyword_reply` | 白名单占位符渲染（`render_template` 的原型）、`context.register_web_api` 面板接法 |
