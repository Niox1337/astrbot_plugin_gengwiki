# astrbot_plugin_gengwiki

让 [AstrBot](https://docs.astrbot.app/) 机器人用上[梗Wiki](https://gengwiki.niox.lol) 的中文网络热梗和表情包。

## 功能

- **回复时用梗**，每次回复前给模型一批梗作为参考，模型只在非常贴切时才用。
- **群里插话**，没人叫它时也可能接一句，只有想到非常贴切的梗才开口。
- **查询工具**，模型可以搜索梗、查看详情和热门榜，也可以搜索并发送表情。

## 安装

需要 AstrBot 4.24 或更高版本，对话模型要支持工具调用，执行方式要用内置 AstrBot AI。

1. 在 AstrBot 的 WebUI 打开插件页，点右下角的 **+**，选择从 URL 安装，填入 `https://github.com/Niox1337/astrbot_plugin_gengwiki`。
2. 在[梗Wiki 个人中心](https://gengwiki.niox.lol/me)创建 API Key，勾选 `memes:read`、`trending:read` 和 `stickers:read`，并关联要用的梗和表情包。注册梗Wiki 目前需要邀请码。
3. 在插件配置里填入 API Key。

每个 AstrBot 用自己的 Key，不要和别人共用。

## 配置

| 配置项 | 含义 | 默认值 |
|---|---|---|
| `api_key` | 梗Wiki API Key | 无 |
| `join_chance_named` | 群友提到梗时插话的概率 | 0.5 |
| `join_chance_any` | 其他群消息插话的概率 | 0.02 |
| `join_cooldown` | 同一个群两次插话的最短间隔（秒） | 300 |

通过概率判定后会调用一次模型，哪怕最后没有开口。不想让它插话，把两个概率都设为 0。

## 许可证

[MIT](LICENSE)
