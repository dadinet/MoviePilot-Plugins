import requests
from typing import Any, List, Dict, Tuple
import threading
import io

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.core.config import settings

lock = threading.Lock()

class TelegramMsg(_PluginBase):
    # 插件名称
    plugin_name = "Telegram消息通知"
    # 插件描述
    plugin_desc = "支持5个Telegram接收消息通知。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/dadinet/MoviePilot-Plugins/main/icons/Telegram_A.png"
    # 插件版本
    plugin_version = "1.4"
    # 插件作者
    plugin_author = "dadinet"
    # 作者主页
    author_url = "https://github.com/dadinet/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "telegrammsg_"
    # 加载顺序
    plugin_order = 32
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _onlyonce = False

    _scheduler = None
    _event = threading.Event()

    def init_plugin(self, config: dict = None):
        """
        初始化插件
        """
        # 读取配置中的 tg_count。如果配置中没有该项，则默认设置为3个 Bot。
        tg_count = config.get("tg_count", 3)

        # 初始化 _tg_configs，根据 tg_count 生成配置
        self._tg_configs = [
            {"chat_id": "", "bot_token": "", "msgtypes": [], "size": i+1}
            for i in range(tg_count)
        ]

        # 如果有 config，更新配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._send_image_enabled = config.get("send_image_enabled")

            # 更新已有的 tg_config
            for i in range(tg_count):
                self._tg_configs[i]["chat_id"] = config.get(f"chat_id_{i+1}", "")
                self._tg_configs[i]["bot_token"] = config.get(f"bot_token_{i+1}", "")
                self._tg_configs[i]["msgtypes"] = config.get(f"msgtypes_{i+1}", [])

        # 如果 onlyonce 为真，发送测试消息
        if self._onlyonce:
            for i, tg_config in enumerate(self._tg_configs):
                if tg_config["chat_id"] and tg_config["bot_token"]:
                    flag = self.send_msg(
                        tg_config,
                        title="Telegram消息通知测试",
                        text="Telegram消息通知测试成功！",
                        image="https://raw.githubusercontent.com/dadinet/MoviePilot-Plugins/main/icons/Telegram_test.png"
                    )
                    if flag:
                        self.systemmessage.put(f"Telegram消息通知测试成功！TG{i + 1}")
            self._onlyonce = False

        self.__update_config()

    def __update_config(self):
        """
        更新配置
        """
        config = {
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "tg_count": len(self._tg_configs),
        }

        for i, tg_config in enumerate(self._tg_configs):
            config[f"chat_id_{i+1}"] = tg_config["chat_id"]
            config[f"bot_token_{i+1}"] = tg_config["bot_token"]
            config[f"msgtypes_{i+1}"] = tg_config["msgtypes"]

        self.update_config(config)


    def get_state(self) -> bool:
        return self._enabled and any(tg_config["bot_token"] and tg_config["chat_id"] for tg_config in self._tg_configs)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 编历 NotificationType 枚举，生成消息类型选项
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.name
            })

        tg_config_form = []
        tg_count = len(self._tg_configs)
        for i in range(tg_count):  # 动态支持多个Telegram账号
            tg_config_form.append({
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 4,
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': f'bot_token_{i+1}',
                                    'label': f'留空不启用',
                                    'placeholder': '123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11',
                                    'hint': 'Telegram Bot token',
                                    'persistent-hint': True,
                                    'clearable': True,
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 4,
                        },
                        'content': [
                            {
                                'component': 'VTextField',
                                'props': {
                                    'model': f'chat_id_{i+1}',
                                    'label': f'留空不启用',
                                    'placeholder': '123456789',
                                    'hint': 'Telegram ID',
                                    'persistent-hint': True,
                                    'clearable': True,
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 4,
                        },
                        'content': [
                            {
                                'component': 'VSelect',
                                'props': {
                                    'multiple': True,
                                    'chips': True,
                                    'model': f'msgtypes_{i+1}',
                                    'label': f'消息类型',
                                    'items': MsgTypeOptions,
                                    'clearable': True,
                                    'hint': '自定义需要接受并发送的消息类型',
                                    'persistent-hint': True,
                                }
                            }
                        ]
                    }
                ]
            })

        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6,
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                            'hint': '开启后插件将处于激活状态',
                                            'persistent-hint': True,
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6,
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立刻发送测试',
                                            'hint': '一次性任务，运行后自动关闭',
                                            'persistent-hint': True,
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6,
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'tg_count',
                                            'label': 'Telegram Bot 数量',
                                            'items': [{'title': str(i), 'value': i} for i in range(1, 6)],
                                            'hint': '选择要配置的Telegram Bot数量',
                                            'persistent-hint': True,
                                            'clearable': False,
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    *tg_config_form,
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '说明：'
                                                    '1、初始显示3个Bot配置，如需多个选择数量保存，再次进入配置即可。'
                                                    '2、由于有的插件用的还是"站点消息"接口，所以目前需要同时勾选“插件消息”和“站点消息”来兼容，等待适配！',
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "tg_count": 3,
            "chat_id_1": "",
            "bot_token_1": "",
            "msgtypes_1": [],
            "chat_id_2": "",
            "bot_token_2": "",
            "msgtypes_2": [],
            "chat_id_3": "",
            "bot_token_3": "",
            "msgtypes_3": [],
            "chat_id_4": "",
            "bot_token_4": "",
            "msgtypes_4": [],
            "chat_id_5": "",
            "bot_token_5": "",
            "msgtypes_5": [],
        }


    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.NoticeMessage)
    def send(self, event: Event):
        """
        消息发送事件
        该方法会在接收到 `NoticeMessage` 类型的事件时被触发。
        它从事件数据中提取消息内容并根据配置发送到相应的Telegram。

        :param event: 事件对象，包含消息的数据。
        """
        # 检查插件是否已启用且Telegram配置有效
        if not self.get_state():
            return

        # 确保事件中包含有效数据
        if not event.event_data:
            return

        # 提取消息内容
        msg_body = event.event_data
        msg_type: NotificationType = msg_body.get("type")  # 消息类型
        title = msg_body.get("title")  # 消息标题
        text = msg_body.get("text")  # 消息文本
        image = msg_body.get("image")  # 消息图片链接

        # 如果标题和内容都为空，则不发送消息
        if not title and not text:
            logger.warn("标题和内容不能同时为空")
            return

        # 遍历每个Telegram配置
        for tg_config in self._tg_configs:
            # 如果Telegram配置中的聊天ID或Bot令牌为空，则跳过该配置
            if not tg_config["chat_id"] or not tg_config["bot_token"]:
                continue

            # 如果消息类型存在且Telegram配置中未启用该消息类型，则跳过该配置
            if msg_type and tg_config["msgtypes"] and msg_type.name not in tg_config["msgtypes"]:
                continue

            # 发送消息到当前配置的Telegram聊天
            self.send_msg(tg_config, title=title, text=text, image=image, msg_type=msg_type)

    def send_msg(self, tg_config, title, text, image=None, msg_type=None):
        """
        发送消息到指定的Telegram聊天。

        :param tg_config: Telegram配置，包含Bot令牌和聊天ID。
        :param title: 消息标题。
        :param text: 消息文本内容。
        :param image: 可选的消息图片URL。
        :param msg_type: 消息类型。
        :return: 如果消息发送成功，则返回True；否则返回False。
        """
        proxies = settings.PROXY if settings.PROXY else None  # 从配置中获取代理设置

        try:
            # 检查Telegram配置中的Bot令牌和聊天ID是否存在
            if not tg_config["bot_token"] or not tg_config["chat_id"]:
                raise Exception("未添加Telegram Bot令牌或聊天ID")

            # 如果内容为空，设置为空字符串而不是 None
            text = text if text else ''

            if image:
                # 如果提供了图片，发送图片消息
                url = f"https://api.telegram.org/bot{tg_config['bot_token']}/sendPhoto"
                # 下载图片内容
                image_content = requests.get(image, proxies=proxies).content
                image_file = io.BytesIO(image_content)
                data = {
                    "chat_id": tg_config["chat_id"],
                    "caption": f"*{title}*\n{text}",
                    "parse_mode": "Markdown"
                }
                files = {
                    "photo": ("image.jpg", image_file)
                }
                # 发送图片消息
                with lock:
                    res = requests.post(url, data=data, files=files, proxies=proxies)
            else:
                # 如果没有图片，则发送文本消息
                url = f"https://api.telegram.org/bot{tg_config['bot_token']}/sendMessage"
                content = f"*{title}*\n{text}"
                data = {
                    "chat_id": tg_config["chat_id"],
                    "text": content,
                    "parse_mode": "Markdown"
                }
                with lock:
                    res = requests.post(url, data=data, proxies=proxies)
            
            # 检查响应
            if res.status_code != 200:
                raise Exception(f"发送失败: {res.text}")

            # 检查响应结果
            if res:
                ret_json = res.json()
                if ret_json.get('ok'):
                    logger.info(f"Bot{tg_config['size']} - {msg_type.value if msg_type else '测试消息'} 消息发送成功")
                else:
                    raise Exception(f"Bot{tg_config['size']} - {msg_type.value if msg_type else '测试消息'} 消息发送失败：{ret_json.get('description')}")
            else:
                raise Exception(f"Bot{tg_config['size']} 消息发送失败，错误码：{res.status_code}，错误原因：{res.reason}")

            return True
        except Exception as msg_e:
            # 捕获异常并记录错误日志
            logger.error(f"Bot{tg_config['size']} 消息发送失败 - {str(msg_e)}", exc_info=True)
            return False

    def stop_service(self):
        """
        退出插件
        停止插件的调度器，清理资源。
        """
        try:
            # 检查并移除调度器中的所有任务
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                # 如果调度器正在运行，停止调度器
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown(wait=False)
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            # 捕获停止服务过程中可能发生的异常并记录错误日志
            logger.error(str(e))