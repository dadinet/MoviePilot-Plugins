import datetime
import re
import traceback
import json
import requests
from typing import Optional, Any, List, Dict, Tuple
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app import schemas
from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo, TorrentInfo, Context
from app.core.metainfo import MetaInfo
from app.db.site_oper import SiteOper
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import ExistMediaInfo
from app.schemas.types import SystemConfigKey, MediaType

class SiteSubscriber(_PluginBase):
    # 插件名称
    plugin_name = "站点资源订阅"
    # 插件描述
    plugin_desc = "定时刷新站点资源,识别内容后添加订阅或直接下载。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/dadinet/MoviePilot-Plugins/refs/heads/main/icons/SiteSubscriber.png"
    # 插件版本
    plugin_version = "1.1"
    # 插件作者
    plugin_author = "dadinet"
    # 作者主页
    author_url = "https://github.com/dadinet"
    # 插件配置项ID前缀
    plugin_config_prefix = "sitesubscriber_"
    # 加载顺序
    plugin_order = 998
    # 可使用的用户级别
    auth_level = 2

    # 私有变量
    _scheduler: Optional[BackgroundScheduler] = None
    downloadchain = None
    searchchain = None
    subscribechain = None

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _notify: bool = False
    _onlyonce: bool = False
    _address: list = []
    _include: str = ""
    _exclude: str = ""
    _clear: bool = False
    _clearflag: bool = False
    _action: str = "subscribe"
    _save_path: str = ""
    _size_range: str = ""
    # 订阅过滤配置
    _quality: str = ""
    _resolution: str = ""
    _effect: str = ""
    _filter_groups: list = []
    _downloader: Optional[str] = None
    _history: Dict[str, dict] = {}
    # 独立通知配置
    _independent_notify: bool = False
    _independent_notify_config: Any = None
    # 日志分组：用于不同资源之间插入空行分隔，提升可读性
    _last_log_group_key: Optional[str] = None

    def init_plugin(self, config: dict = None):
        self.downloadchain = DownloadChain()
        self.searchchain = SearchChain()
        self.subscribechain = SubscribeChain()

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self.__validate_and_fix_config(config=config)
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._address = config.get("address")
            self._include = config.get("include")
            self._exclude = config.get("exclude")
            self._clear = config.get("clear")
            self._action = config.get("action")
            self._save_path = config.get("save_path")
            self._size_range = config.get("size_range")
            # 加载新增的订阅过滤配置
            self._quality = config.get("quality")
            self._resolution = config.get("resolution")
            self._effect = config.get("effect")
            self._filter_groups = config.get("filter_groups")
            self._downloader = config.get("downloader")
            # 加载独立通知配置
            self._independent_notify = config.get("independent_notify") or False
            self._independent_notify_config = config.get("independent_notify_config")

        # 加载历史记录
        self._history = self.get_data('history') or {}

        # 配置保存后立即执行一次，通常用于手动触发
        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info(f"站点资源订阅服务启动，准备立即运行一次，站点: {self._address}")
            self._scheduler.add_job(func=self.check, trigger='date',
                                    run_date=datetime.datetime.now(
                                        tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                    )

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

        # 清理与一次性运行的状态复位：避免下次启动仍处于该状态
        if self._onlyonce or self._clear:
            # 关闭一次性开关
            self._onlyonce = False
            # 记录清理缓存设置
            self._clearflag = self._clear
            # 关闭清理缓存开关
            self._clear = False
            # 保存设置
            self.__update_config()

    def get_state(self) -> bool:
        return self._enabled


    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        """
        return [
            {
                "path": "/confirm_item",
                "endpoint": self.confirm_item,
                "methods": ["GET"],
                "summary": "确认待办事项"
            },
            {
                "path": "/ignore_item",
                "endpoint": self.ignore_item,
                "methods": ["GET"],
                "summary": "忽略待办事项"
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self._enabled and self._cron:
            return [{
                "id": "SiteSubscriber",
                "name": "站点资源订阅服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.check,
                "kwargs": {}
            }]
        elif self._enabled:
            return [{
                "id": "SiteSubscriber",
                "name": "站点资源订阅服务",
                "trigger": "interval",
                "func": self.check,
                "kwargs": {"minutes": 30}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面
        """
        # 硬编码常见的选项
        quality_items = ['全部', '蓝光原盘', 'Remux', 'BluRay', 'UHD', 'WEB-DL', 'HDTV', 'H265', 'H264']
        resolution_items = ['全部', '4k', '1080p', '720p']
        effect_items = ['全部', '杜比视界', '杜比全景声', 'HDR', 'SDR']
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VTextField', 'props': {'model': 'cron', 'label': '执行周期', 'placeholder': '5位cron表达式，留空自动'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VSelect', 'props': {'model': 'action', 'label': '动作', 'items': [{'title': '手动订阅', 'value': 'manual_subscribe'}, {'title': '自动订阅', 'value': 'auto_subscribe'}, {'title': '下载', 'value': 'download'}]}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'address',
                                            'label': '选择站点',
                                            'items': [
                                                {'title': site.name, 'value': site.id}
                                                for site in SiteOper().list()
                                                if site.id in (SystemConfigOper().get(SystemConfigKey.RssSites) or [])
                                            ],
                                            'multiple': True,
                                            'chips': True,
                                            'closable-chips': True,
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSelect', 'props': {'model': 'quality', 'label': '质量', 'items': quality_items}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSelect', 'props': {'model': 'resolution', 'label': '分辨率', 'items': resolution_items}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSelect', 'props': {'model': 'effect', 'label': '特效', 'items': effect_items}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'filter_groups',
                                            'label': '优先级规则组',
                                            'items': self.get_rule_groups(),
                                            'multiple': True,
                                            'chips': True,
                                            'closable-chips': True,
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'downloader',
                                            'label': '下载器',
                                            'items': [{'title': '默认', 'value': None}] + self.get_downloader_for_select(),
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'include', 'label': '包含', 'placeholder': '支持正则表达式'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'exclude', 'label': '排除', 'placeholder': '支持正则表达式'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'size_range', 'label': '种子大小(GB)', 'placeholder': '如：3 或 3-5'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'save_path', 'label': '保存目录', 'placeholder': '留空则使用系统默认设置'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'independent_notify', 'label': '独立通知'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'notify_dialog_open', 'label': '打开独立通知设置窗口'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'clear', 'label': '清理历史记录'}}
                                ]
                            }
                        ]
                    }
                    ,
                    {
                        "component": "VDialog",
                        "props": {
                            "model": "notify_dialog_open",
                            "max-width": "65rem",
                            "overlay-class": "v-dialog--scrollable v-overlay--scroll-blocked",
                            "content-class": "v-card v-card--density-default v-card--variant-elevated rounded-t"
                        },
                        "content": [
                            {
                                "component": "VCard",
                                "props": {
                                    "title": "设置独立通知配置"
                                },
                                "content": [
                                    {
                                        "component": "VDialogCloseBtn",
                                        "props": {
                                            "model": "notify_dialog_open"
                                        }
                                    },
                                    {
                                        "component": "VCardText",
                                        "props": {},
                                        "content": [
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
                                                                'component': 'VAceEditor',
                                                                'props': {
                                                                    'modelvalue': 'independent_notify_config',
                                                                    'lang': 'json',
                                                                    'theme': 'monokai',
                                                                    'style': 'height: 30rem',
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
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'VAlert',
                                                                'props': {
                                                                    'type': 'info',
                                                                    'variant': 'tonal',
                                                                    'text': '说明：目前仅支持Telegram通知。'
                                                                }
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False, "notify": True, "onlyonce": False, "cron": "*/30 * * * *",
            "address": [], "include": "", "exclude": "", "quality": "全部", "resolution": "全部",
            "effect": "全部", "filter_groups": [], "downloader": None,
            "clear": False, "action": "manual_subscribe", "save_path": "", "size_range": "",
            "independent_notify": False, "notify_dialog_open": False,
            "independent_notify_config": """[\n    {\n        \"channel\": \"telegram\",\n        \"token\": \"123456:ABC-DEF1234567890\",\n        \"chat_id\": \"-1001234567890\",\n        \"proxy\": true\n    }\n]"""
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面
        """
        # 仅展示状态为 pending 的待办项，由新逻辑保证每项都含唯一键 key
        pending_list = [item for item in self._history.values() if item.get("status") == "pending"]
        logger.info(f"待确认列表：{pending_list}")
        
        if not pending_list:
            return [{'component': 'div', 'text': '暂无待确认数据', 'props': {'class': 'text-center'}}]

        pending_list = sorted(pending_list, key=lambda x: x.get('time'), reverse=True)
        contents = []
        for item in pending_list:
            item_key = item.get("key")
            # 计算展示用集数状态
            total_eps = item.get('total_episodes') or 0
            latest_ep = item.get('latest_episode') or 0
            status_text = ''
            if total_eps and latest_ep and latest_ep >= total_eps:
                status_text = f"完结({total_eps})"
            elif total_eps and latest_ep:
                status_text = f"({latest_ep}//{total_eps})"
            elif total_eps:
                status_text = f"完结({total_eps})"
            # 根据状态选择徽标颜色
            status_color = None
            if status_text:
                status_color = 'success' if status_text.startswith('完结') else 'warning'
            # 圆点颜色（不使用组件色名，明确到具体颜色值）
            status_dot_color = None
            if status_color == 'success':
                status_dot_color = '#4CAF50'
            elif status_color == 'warning':
                status_dot_color = '#FF9800'
            contents.append({
                'component': 'VCard',
                'props': {
                    'image': item.get("mediainfo", {}).get('backdrop_path'),
                    'class': 'flex flex-col h-full',
                    'style': 'min-height: 140px'
                },
                'content': [
                    {
                        'component': 'div',
                        'props': {'class': 'absolute inset-0', 'style': 'background-image: linear-gradient(to top, rgba(0,0,0,0.9), rgba(0,0,0,0.5));'}
                    },
                    *([
                        {
                            'component': 'div',
                            'props': {
                                'class': 'absolute right-2 top-2 z-20 flex items-center gap-2 px-2 py-1 rounded',
                                'style': 'background-color: rgba(0,0,0,0.45);'
                            },
                            'content': [
                                {
                                    'component': 'div',
                                    'props': {'class': 'text-white text-caption'},
                                    'text': status_text
                                },
                                {
                                    'component': 'div',
                                    'props': {
                                        'style': f'width: 14px; height: 14px; border-radius: 9999px; background-color: {status_dot_color};'
                                    }
                                }
                            ]
                        }
                    ] if status_text else []),
                    {
                        'component': 'div',
                        'props': {'class': 'relative z-10'},
                        'content': [
                            {
                                'component': 'div',
                                'props': {'class': 'v-card-text flex items-center pt-3 pb-2'},
                                'content': [
                                    {
                                        'component': 'div',
                                        'props': {'class': 'h-auto w-16 flex-shrink-0 overflow-hidden rounded-md'},
                                        'content': [
                                            {'component': 'VImg', 'props': {'src': item.get("poster"), 'aspect-ratio': '2/3', 'cover': True}}
                                        ]
                                    },
                                    {
                                        'component': 'div',
                                        'props': {'class': 'flex flex-col justify-center overflow-hidden pl-2 xl:pl-4'},
                                        'content': [
                                            {'component': 'div', 'props': {'class': 'text-sm font-medium text-white sm:pt-1'}, 'text': item.get('mediainfo', {}).get('year')},
                                            {'component': 'div', 'props': {'class': 'mr-2 min-w-0 text-lg font-bold text-white text-ellipsis overflow-hidden line-clamp-2'}, 'text': f"{item.get('mediainfo', {}).get('title')}{f' S{str(item.get('meta', {}).get('season')).zfill(2)}' if item.get('meta', {}).get('season') else ''}"},
                                            {'component': 'div', 'props': {'class': 'text-subtitle-2 text-white'}, 'text': f'{item.get("type")}'},
                                            {'component': 'div', 'props': {'class': 'text-subtitle-2 text-white'}, 'text': f'{item.get("time")}'},
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'div',
                                'props': {'class': 'd-flex ga-2 pa-2 justify-center'},
                                'content': [
                                    {
                                        'component': 'VBtn',
                                        'props': {'color': 'primary'},
                                        'text': '下载' if item.get("action") == "download" else '订阅',
                                        'events': {
                                            'click': {
                                                'api': 'plugin/SiteSubscriber/confirm_item', 'method': 'get',
                                                'params': {'key': item_key, 'apikey': settings.API_TOKEN},
                                                'refresh': True
                                            }
                                        }
                                    },
                                    {
                                        'component': 'VBtn',
                                        'props': {'color': 'error'},
                                        'text': '忽略',
                                        'events': {
                                            'click': {
                                                'api': 'plugin/SiteSubscriber/ignore_item', 'method': 'get',
                                                'params': {'key': item_key, 'apikey': settings.API_TOKEN},
                                                'refresh': True
                                            }
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            })
        return [{'component': 'div', 'props': {'class': 'grid gap-3 grid-info-card'}, 'content': contents}]

    def __send_independent_notification(self, title: str, text: str, image: Optional[str] = None,
                                        poster: Optional[str] = None, overview: Optional[str] = None,
                                        links: Optional[List[Dict[str, str]]] = None) -> bool:
        """
        使用独立通知设置发送通知。返回是否已成功发送。
        仅当开启了独立通知且配置有效时生效；当前仅支持 Telegram。
        """
        try:
            if not self._independent_notify:
                return False
            config_value = self._independent_notify_config
            if not config_value:
                return False
            if isinstance(config_value, str):
                try:
                    notify_confs = json.loads(config_value)
                except Exception as err:
                    logger.error(f"独立通知配置解析失败：{err}")
                    return False
            else:
                notify_confs = config_value

            if not isinstance(notify_confs, list):
                logger.error("独立通知配置格式错误，应为数组")
                return False

            # 拼装通知正文：当前仅包含标题与文本，避免平台差异带来的失败
            message_lines = [title, text]
            message = "\n".join([line for line in message_lines if line])

            any_sent = False
            for conf in notify_confs:
                channel = (conf or {}).get("channel")
                if not channel:
                    continue
                if channel.lower() == "telegram":
                    token = conf.get("token")
                    chat_id = conf.get("chat_id")
                    use_proxy = conf.get("proxy")
                    if not token or not chat_id:
                        logger.warning("独立通知 Telegram 配置缺少 token 或 chat_id，已跳过")
                        continue
                    # 根据全局代理设置可选地构造 requests 代理
                    proxies = None
                    if use_proxy and getattr(settings, "PROXY", None):
                        proxy_value = settings.PROXY
                        if isinstance(proxy_value, str):
                            proxies = {"http": proxy_value, "https": proxy_value}
                        elif isinstance(proxy_value, dict):
                            proxies = proxy_value
                    photo_url = image or poster
                    try:
                        if photo_url:
                            url = f"https://api.telegram.org/bot{token}/sendPhoto"
                            payload = {"chat_id": chat_id, "photo": photo_url, "caption": message}
                            resp = requests.post(url, data=payload, timeout=10, proxies=proxies)
                        else:
                            url = f"https://api.telegram.org/bot{token}/sendMessage"
                            payload = {"chat_id": chat_id, "text": message}
                            resp = requests.post(url, data=payload, timeout=10, proxies=proxies)
                        if resp.ok:
                            any_sent = True
                        else:
                            logger.error(f"Telegram 发送失败：{resp.status_code} {resp.text}")
                    except Exception as send_err:
                        logger.error(f"Telegram 发送异常：{send_err}")
                else:
                    logger.warning(f"不支持的独立通知通道：{channel}")

            return any_sent
        except Exception as e:
            logger.error(f"独立通知发送失败：{e}")
            return False

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    def confirm_item(self, key: str, apikey: str):
        """
        确认待办事项
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        
        # 使用历史唯一键精确定位待办项
        item_to_process = self._history.get(key)
        if not item_to_process or item_to_process.get("status") != "pending":
            logger.error(f"确认失败：未在历史记录中找到待办事项 - key: {key}")
            return schemas.Response(success=False, message="未找到指定的待办事项")

        logger.info(f"开始确认项目：{item_to_process.get('title')}")

        try:
            action = item_to_process.get("action")
            site_id = item_to_process.get("site_id")
            meta_dict = item_to_process.get("meta", {})
            meta = MetaInfo(title=meta_dict.get("name"))
            meta.year = meta_dict.get("year")
            meta.type = MediaType(meta_dict.get("type")) if meta_dict.get("type") else None

            # 优先使用历史里的 season；无则尝试从标题解析
            if meta_dict.get("season") is not None:
                meta.begin_season = meta_dict.get("season")
            else:
                season = self._get_season_from_title(item_to_process.get("title"))
                if season:
                    meta.begin_season = season
            mediainfo = MediaInfo()
            mediainfo.from_dict(item_to_process.get("mediainfo", {}))

            torrent_info = TorrentInfo()
            torrent_info.from_dict(item_to_process.get("torrent_info", {}))

            # 若标题或描述包含“全N集”，若仍无季号则默认视作第1季（不再写入 episode_list 到 meta）
            combined_text = f"{item_to_process.get('title') or ''} {getattr(torrent_info, 'description', '') or ''}"
            total_eps = self._get_total_episodes_from_title(combined_text)
            if total_eps and getattr(meta, "begin_season", None) is None:
                meta.begin_season = 1

            logger.info(f"动作：{self._get_action_cn(action)}，站点ID：{site_id}")

            if action == "download":
                logger.info("执行下载...")
                self.download_torrent(meta=meta, mediainfo=mediainfo, torrent_info=torrent_info)
            elif action == "manual_subscribe":
                logger.info("执行手动订阅...")
                if self.subscribechain.exists(mediainfo=mediainfo, meta=meta):
                    logger.info(f"'{mediainfo.title_year} {meta.season}' 已在订阅中")
                else:
                    self.add_subscribe(meta=meta, mediainfo=mediainfo, site_id=site_id)
            
            logger.info("操作执行完毕，更新状态...")
            # 更新状态为 confirmed 并持久化
            self._history[key]["status"] = "confirmed"
            self.save_data('history', self._history)
            logger.info("状态更新并保存成功")

            return schemas.Response(success=True, message="操作成功")
        except Exception as e:
            logger.error(f"处理待办事项出错：{str(e)} - {traceback.format_exc()}")
            return schemas.Response(success=False, message=f"操作失败：{str(e)}")

    def ignore_item(self, key: str, apikey: str):
        """
        忽略待办事项
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")

        # 使用历史唯一键精确定位待办项
        item_to_ignore = self._history.get(key)
        if not item_to_ignore or item_to_ignore.get("status") != "pending":
            return schemas.Response(success=False, message="未找到指定的待办事项")

        mediainfo_dict = item_to_ignore.get('mediainfo', {})
        meta_dict = item_to_ignore.get('meta', {})
        log_title = self._get_log_title(mediainfo_dict, meta_dict)
        logger.info(f"正在忽略项目：{log_title}")
        self._history[key]["status"] = "ignored"
        self.save_data('history', self._history)
        logger.info(f"'{log_title}' 已被忽略")

        return schemas.Response(success=True, message="忽略成功")

    def __update_config(self):
        """
        更新设置
        """
        self.update_config({
            "enabled": self._enabled, "notify": self._notify, "onlyonce": self._onlyonce,
            "cron": self._cron, "address": self._address, "include": self._include,
            "exclude": self._exclude, "clear": self._clear,
            "action": self._action, "save_path": self._save_path,
            "size_range": self._size_range, "quality": self._quality, "resolution": self._resolution,
            "effect": self._effect, "filter_groups": self._filter_groups, "downloader": self._downloader,
            "independent_notify": self._independent_notify,
            "independent_notify_config": self._independent_notify_config
        })

    def check(self):
        """
        通过站点获取数据并处理
        """
        # 每轮运行重置日志分组键
        self._last_log_group_key = None
        logger.info(f"站点资源订阅 check 任务开始执行，站点: {self._address}，动作: {self._get_action_cn(self._action)}")
        if not self._address:
            logger.warning("站点列表为空，任务结束。")
            return

        # 若设置了清理开关，先清空历史并重置标志位
        if self._clearflag:
            self._history = {}
            self.save_data('history', self._history)
        
        # 仅保留有效的过滤项（空或“全部”不参与）
        filter_params = {
            key: value for key, value in {
                "include": self._include, "exclude": self._exclude, "quality": self._quality,
                "resolution": self._resolution, "effect": self._effect,
            }.items() if value and value != '全部'
        }
        logger.info(f"将使用以下参数进行过滤: {filter_params}")
        logger.info(f"将使用以下优先级规则组进行过滤: {self._filter_groups}")

        torrent_helper = TorrentHelper()

        for site_id in self._address:
            if not site_id:
                continue
            logger.info(f"开始处理站点：{site_id} ...")

            contexts = self.searchchain.search_by_title(title="", sites=[site_id])
            if not contexts:
                logger.error(f"未从站点 {site_id} 获取到数据")
                continue

            for context in contexts:
                try:
                    self._process_torrent(context=context, site_id=site_id,
                                          filter_params=filter_params, torrent_helper=torrent_helper)
                except Exception as err:
                    logger.error(f'处理种子信息出错：{str(err)} - {traceback.format_exc()}')
            
            logger.info(f"站点 {site_id} 处理完成")

        self.save_data('history', self._history)
        self._clearflag = False

    def _process_torrent(self, context: Context, site_id: str, filter_params: dict, torrent_helper: TorrentHelper):
        """
        处理单个种子
        """
        torrent_info = context.torrent_info
        if not torrent_info:
            return
        # 不同资源之间插入空行分隔（按完整标题分组）
        try:
            current_key = (torrent_info.title or "").strip()
            if current_key and self._last_log_group_key != current_key:
                if self._last_log_group_key is not None:
                    logger.info("")
                self._last_log_group_key = current_key
                # 在新资源块开始时先打印完整资源名，便于阅读检索
                logger.info(f"{torrent_info.title}")
        except Exception:
            pass

        # 1) 属性过滤（标题、质量、分辨率、特效等）
        if not torrent_helper.filter_torrent(torrent_info, filter_params):
            logger.info(f"'{torrent_info.title}' 不符合属性过滤规则，已跳过")
            return

        # 2) 元信息识别，尽量提取季号；未识别到媒体名则放弃
        meta = MetaInfo(title=torrent_info.title, subtitle=torrent_info.description)
        season = self._get_season_from_title(torrent_info.title)
        if season:
            meta.begin_season = season
        # 若标题或描述包含“全N集”，若仍无季号则默认视作第1季（不写入只读属性）
        total_eps = self._get_total_episodes_from_title(f"{torrent_info.title} {torrent_info.description or ''}")
        if total_eps and getattr(meta, "begin_season", None) is None:
            meta.begin_season = 1
        if not meta.name:
            logger.warning(f"'{torrent_info.title}' 未识别到有效媒体名称，无法应用优先级规则组")
            return
        mediainfo: MediaInfo = self.searchchain.recognize_media(meta=meta)
        if not mediainfo:
            logger.warning(f"未识别到媒体信息: '{torrent_info.title}'，无法应用优先级规则组")
            return
        # 打印从 mediainfo 推断的总集数，来源明确
        try:
            season_no = getattr(meta, 'begin_season', None)
            mi_total = self._get_total_episodes_from_mediainfo(mediainfo, season_no)
            logger.info(f"mediainfo - 媒体数据总集数: {mi_total or '-'}")
        except Exception:
            pass

        # 3) 规则组过滤（用户配置的更细粒度优先规则）
        if self._filter_groups:
            filtered_torrents = self.searchchain.filter_torrents(
                rule_groups=self._filter_groups,
                torrent_list=[torrent_info],
                mediainfo=mediainfo
            )
            if not filtered_torrents:
                logger.info(f"'{torrent_info.title}' 不匹配优先级规则组，已跳过")
                return
            torrent_info = filtered_torrents[0]

        # 4) 构造历史唯一键与标准日志标题，用于去重与用户可读日志
        history_key = self._get_history_key(mediainfo, meta)
        log_title = self._get_log_title(mediainfo.to_dict(), meta)

        if history_key and self._history.get(history_key):
            existing = self._history[history_key]
            status_map = {"pending": "待确认", "confirmed": "已确认", "ignored": "已忽略"}
            status = existing.get("status")
            status_cn = status_map.get(status, "未知")
            if status == "pending":
                # 仅更新 pending 的统计信息（优先：标题/描述“全N集” > mediainfo > episode_list；避免回退）
                prev_total = existing.get("total_episodes")
                prev_latest = existing.get("latest_episode")
                display_total, latest_ep = self._compute_episode_stats(
                    meta=meta,
                    mediainfo=mediainfo,
                    torrent_info=torrent_info,
                    prev_total=prev_total,
                    prev_latest=prev_latest
                )
                # 仅当发生变化时写回并打印更新日志
                prev_total_int = int(prev_total) if isinstance(prev_total, int) else (int(prev_total) if isinstance(prev_total, str) and prev_total.isdigit() else 0)
                prev_latest_int = int(prev_latest) if isinstance(prev_latest, int) else (int(prev_latest) if isinstance(prev_latest, str) and prev_latest.isdigit() else 0)
                new_total_int = int(display_total) if display_total else 0
                new_latest_int = int(latest_ep) if latest_ep else 0
                changed = (new_total_int != prev_total_int) or (new_latest_int != prev_latest_int)
                if changed:
                    if display_total:
                        existing["total_episodes"] = display_total
                    if latest_ep:
                        existing["latest_episode"] = latest_ep
                    # 更新最近一次统计更新时间
                    existing["time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._history[history_key] = existing
                    self.save_data('history', self._history)
                    logger.info(f"'{log_title}' 已存在且为 待确认，已更新统计信息 (总集数={display_total or '-'}, 最新集数={latest_ep or '-'})")
            else:
                logger.info(f"'{log_title}' 已存在于历史记录中 (状态: {status_cn})，不更新")
            return

        # 5) 计算用于存在性判断的集清单（不写入 meta，避免只读属性异常）
        computed_episode_list: Optional[List[int]] = None
        if total_eps:
            computed_episode_list = list(range(1, total_eps + 1))
        elif getattr(meta, "begin_season", None) is not None:
            mi_total = self._get_total_episodes_from_mediainfo(mediainfo, meta.begin_season)
            if mi_total:
                computed_episode_list = list(range(1, mi_total + 1))

        # 6) 尺寸过滤：配置为 GB，转为字节与种子 size 对比
        if self._size_range and torrent_info.size:
            sizes = [float(_size) * 1024 ** 3 for _size in self._size_range.split("-")]
            if (len(sizes) == 1 and float(torrent_info.size) < sizes[0]) or \
               (len(sizes) > 1 and not sizes[0] <= float(torrent_info.size) <= sizes[1]):
                logger.info(f"'{torrent_info.title}' - 种子大小不符合条件，已跳过处理")
                return

        # 7) 存量检查：媒体库存在或订阅已存在则跳过
        exists_full, complete_flag = self.media_exists_check(
            mediainfo=mediainfo, meta=meta, episode_list=computed_episode_list
        )
        if exists_full:
            suffix = "（无缺集）" if mediainfo.type == MediaType.TV and complete_flag else ""
            logger.info(f"'{log_title}' 在媒体库中已存在{suffix}，已跳过处理")
            return

        if self.subscribechain.exists(mediainfo=mediainfo, meta=meta):
            logger.info(f"'{log_title}' 已在订阅中，已跳过处理")
            return

        # 8) 最终动作：自动订阅 / 直接下载 / 加入待办
        if self._action == "auto_subscribe":
            logger.info(f"'{log_title}' 不在订阅中，开始自动订阅")
            self.add_subscribe(meta=meta, mediainfo=mediainfo, site_id=site_id)
        elif self._action == "download":
            self.download_torrent(meta=meta, mediainfo=mediainfo, torrent_info=torrent_info)
        else:
            # 手动订阅：存入待办（meta 精简为可序列化字段，避免 Tokens 等对象导致保存失败）
            safe_meta = {
                "name": getattr(meta, "name", None),
                "year": getattr(meta, "year", None),
                "type": (mediainfo.type.value if getattr(mediainfo, "type", None) else (meta.type.value if getattr(meta, "type", None) else None)),
                "season": getattr(meta, "begin_season", None),
            }
            # 统计展示：总集数与最新集数（优先“全N集”，次之 mediainfo，再其次 episode_list）
            display_total, latest_ep = self._compute_episode_stats(meta=meta, mediainfo=mediainfo, torrent_info=torrent_info)
            history_item = {
                "title": torrent_info.title,
                "poster": mediainfo.get_poster_image(),
                "type": mediainfo.type.value,
                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "pending",
                "action": self._action,
                "site_id": site_id,
                "meta": safe_meta,
                "mediainfo": mediainfo.to_dict(),
                "torrent_info": torrent_info.to_dict(),
                "total_episodes": display_total if display_total else None,
                "latest_episode": latest_ep if latest_ep else None,
                "key": history_key
            }
            if history_key:
                self._history[history_key] = history_item
                self.save_data('history', self._history)
                # 新增时也打印一次统计信息
                stats_msg = ""
                if display_total or latest_ep:
                    stats_msg = f" (总集数={display_total or '-'}, 最新集数={latest_ep or '-'})"
                logger.info(f"'{log_title}' 已添加到待确认列表{stats_msg}")
                if self._notify:
                    text = f"{log_title} 已添加到待确认列表，请及时处理。"
                    if self._independent_notify:
                        self.__send_independent_notification(
                            title="新的待办订阅", text=text,
                            image=mediainfo.get_backdrop_image(),
                            poster=mediainfo.get_poster_image(),
                            overview=mediainfo.overview
                        )
                    else:
                        self.post_message(
                            mtype="订阅", title="新的待办订阅", text=text,
                            image=mediainfo.get_backdrop_image(),
                            poster=mediainfo.get_poster_image(),
                            overview=mediainfo.overview
                        )

    def media_exists_check(self, mediainfo: MediaInfo, meta: MetaInfo, episode_list: Optional[List[int]] = None) -> Tuple[bool, bool]:
        # 查询媒体是否已存在：电影看整体是否存在，剧集按季与集做“子集”判定
        exist_info: Optional[ExistMediaInfo] = self.searchchain.media_exists(mediainfo=mediainfo)
        if mediainfo.type == MediaType.TV:
            if not exist_info or not getattr(exist_info, 'seasons', None):
                return False, False
            exist_episodes = exist_info.seasons.get(meta.begin_season)
            if not exist_episodes:
                return False, False
            check_list = episode_list if episode_list is not None else getattr(meta, 'episode_list', None)
            if check_list:
                complete = set(check_list).issubset(set(exist_episodes))
                return complete, complete
            return False, False
        return bool(exist_info), bool(exist_info)

    def download_torrent(self, meta: MetaInfo, mediainfo: MediaInfo, torrent_info: TorrentInfo):
        self.downloadchain.download_single(
            context=Context(meta_info=meta, media_info=mediainfo, torrent_info=torrent_info),
            save_path=self._save_path,
            downloader=self._downloader,
            username="站点资源订阅"
        )

    def add_subscribe(self, meta: MetaInfo, mediainfo: MediaInfo, site_id: str):
        quality = self._quality if self._quality and self._quality != '全部' else ""
        resolution = self._resolution if self._resolution and self._resolution != '全部' else ""
        effect = self._effect if self._effect and self._effect != '全部' else ""
        self.subscribechain.add(
            title=mediainfo.title, year=mediainfo.year, mtype=mediainfo.type,
            tmdbid=mediainfo.tmdb_id, season=meta.begin_season, exist_ok=True,
            username="站点资源订阅", downloader=self._downloader, save_path=self._save_path,
            quality=quality, resolution=resolution, effect=effect,
            filter_groups=self._filter_groups, include=self._include, exclude=self._exclude,
            sites=[site_id]
        )

    def __log_and_notify_error(self, message):
        logger.error(message)
        self.systemmessage.put(message, title="站点资源订阅")

    def __validate_and_fix_config(self, config: dict = None) -> bool:
        size_range = config.get("size_range")
        if size_range and not self.__is_number_or_range(str(size_range)):
            self.__log_and_notify_error(f"站点资源订阅出错，种子大小设置错误：{size_range}")
            config["size_range"] = None
            return False
        return True

    @staticmethod
    def _get_action_cn(action: Optional[str]) -> str:
        mapping = {
            "manual_subscribe": "手动订阅",
            "auto_subscribe": "自动订阅",
            "download": "下载",
        }
        return mapping.get(action, action or "")

    @staticmethod
    def _get_season_from_title(title: str) -> Optional[int]:
        """
        从标题中提取季号
        """
        if not title:
            return None
        # 支持 S01 / 开头 S01 / 第1季 / Season 1 等常见写法
        season_match = re.search(r'(?:^|[.\s_-])S(\d+)', title, re.I)
        if not season_match:
            season_match = re.search(r'第(\d+)季', title, re.I)
        if not season_match:
            season_match = re.search(r'\bSeason[ .]?(\d+)', title, re.I)
        if season_match:
            return int(season_match.group(1))
        return None

    @staticmethod
    def _get_total_episodes_from_title(title: str) -> Optional[int]:
        """
        从标题中解析“全N集”的总集数
        """
        if not title:
            return None
        match = re.search(r'全0*(\d+)集', title)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _get_latest_episode_from_text(text: str) -> Optional[int]:
        """
        从文本中解析已发布的最新集数，支持以下写法，出现多个时取最大值：
        - S01E20 / S01E20-E21 / E20 / E20-21 / EP20 / EP20-21
        - 第20集 / 第20-21集 / 第20、21集
        """
        if not text:
            return None
        candidates: List[int] = []
        # 避免把“修复/替换/补发 第N集”这类维护说明当作最新进度
        exclusion_keywords = ["修复", "修正", "补发", "补档", "重发", "替换", "修补", "补齐", "补种"]

        def is_excluded(prefix: str) -> bool:
            return any(kw in prefix for kw in exclusion_keywords)

        # 1) 形如 S01E20 或 S01E20-E21
        for m in re.finditer(r'S\s*\d+\s*E\s*(\d+)(?:\s*[\-~]\s*(?:E)?\s*(\d+))?', text, re.IGNORECASE):
            prefix = text[max(0, m.start()-8):m.start()]
            if is_excluded(prefix):
                continue
            try:
                first_ep = int(m.group(1))
                candidates.append(first_ep)
                if m.group(2):
                    candidates.append(int(m.group(2)))
            except Exception:
                pass

        # 2) 独立的 E20 / E20-21（避免匹配到 HEVC 等，使用前后界定）
        for m in re.finditer(r'(?<![A-Za-z0-9])E\s*(\d+)(?:\s*[\-~]\s*(?:E)?\s*(\d+))?\b', text, re.IGNORECASE):
            prefix = text[max(0, m.start()-8):m.start()]
            if is_excluded(prefix):
                continue
            try:
                candidates.append(int(m.group(1)))
                if m.group(2):
                    candidates.append(int(m.group(2)))
            except Exception:
                pass

        # 3) EP20 / EP20-21
        for m in re.finditer(r'(?<![A-Za-z0-9])EP\s*(\d+)(?:\s*[\-~]\s*(?:EP)?\s*(\d+))?\b', text, re.IGNORECASE):
            prefix = text[max(0, m.start()-8):m.start()]
            if is_excluded(prefix):
                continue
            try:
                candidates.append(int(m.group(1)))
                if m.group(2):
                    candidates.append(int(m.group(2)))
            except Exception:
                pass

        # 4) 第20集 / 第20-21集 / 第20、21集
        for m in re.finditer(r'第\s*(\d+)\s*(?:[\-~、,，]\s*(\d+)\s*)?集', text):
            prefix = text[max(0, m.start()-8):m.start()]
            if is_excluded(prefix):
                continue
            try:
                candidates.append(int(m.group(1)))
                if m.group(2):
                    candidates.append(int(m.group(2)))
            except Exception:
                pass
        return max(candidates) if candidates else None

    @staticmethod
    def _get_total_episodes_from_mediainfo(mediainfo: MediaInfo, season: Optional[int]) -> Optional[int]:
        """
        尝试从 mediainfo 中解析当前季的总集数。该方法对键名与结构做了尽量宽松的兼容。
        """
        try:
            if not mediainfo:
                return None
            info_dict = mediainfo.to_dict() if hasattr(mediainfo, "to_dict") else None
            if not info_dict:
                return None
            # 直接键名
            for key in ["total_episodes", "episode_count", "episodes_count"]:
                value = info_dict.get(key)
                if isinstance(value, int) and value > 0:
                    return value
            # seasons 结构（可能是 dict 或 list）
            seasons_data = info_dict.get("seasons")
            if not seasons_data or season is None:
                return None
            # dict: 处理多种可能的键
            if isinstance(seasons_data, dict):
                for k in [season, str(season), f"S{str(season).zfill(2)}", f"{season:02d}"]:
                    if k in seasons_data:
                        data = seasons_data.get(k)
                        if isinstance(data, list):
                            return len(data)
                        if isinstance(data, dict):
                            for ckey in ["episode_count", "episodes", "total_episodes"]:
                                v = data.get(ckey)
                                if isinstance(v, int) and v > 0:
                                    return v
                                if isinstance(v, list):
                                    return len(v)
                        if isinstance(data, int) and data > 0:
                            return data
            # list: 形如 [{season_number, episode_count, ...}]
            if isinstance(seasons_data, list):
                for item in seasons_data:
                    if not isinstance(item, dict):
                        continue
                    sn = item.get("season_number") or item.get("season") or item.get("number")
                    if sn == season:
                        for ckey in ["episode_count", "episodes", "total_episodes"]:
                            v = item.get(ckey)
                            if isinstance(v, int) and v > 0:
                                return v
                            if isinstance(v, list):
                                return len(v)
            return None
        except Exception:
            return None

    def _compute_episode_stats(self, meta: MetaInfo, mediainfo: MediaInfo, torrent_info: TorrentInfo, prev_total: Optional[int] = None, prev_latest: Optional[int] = None) -> Tuple[Optional[int], Optional[int]]:
        """
        计算展示用的总集数与最新集数：
        - 总集数优先取标题/描述中的“全N集”，其次取 mediainfo，最后取 episode_list 长度
        - 若存在历史总集数，则不降低（取更大者）
        - 若仅识别到总集数，则最新集数默认为总集数
        - 若最新集数大于总集数，则总集数取两者较大值，避免矛盾
        """
        combined_text = f"{torrent_info.title} {torrent_info.description or ''}"
        title_total = self._get_total_episodes_from_title(combined_text) or 0
        mi_total = self._get_total_episodes_from_mediainfo(mediainfo, getattr(meta, "begin_season", None)) or 0
        list_total = len(getattr(meta, "episode_list", []) or [])

        # 按优先级选择，并与历史值取较大
        total = title_total or mi_total or list_total or 0
        if prev_total:
            total = max(total, int(prev_total))

        latest = self._get_latest_episode_from_text(combined_text)
        # 维持单调不减：若此前已有最新集数，且本次识别到的更小，则保留较大值
        if prev_latest and (not latest or latest < int(prev_latest)):
            latest = int(prev_latest)
        if not latest and total:
            latest = total
        # 若标题/描述明确出现“全N集”，优先将最新集数提升为总集数，避免被“修复第2集”等维护信息干扰
        if title_total and total:
            latest = max(latest or 0, total)

        if latest and total and latest > total:
            total = latest

        return (total or None, latest or None)

    @staticmethod
    def _get_history_key(mediainfo: MediaInfo, meta: MetaInfo) -> Optional[str]:
        """
        生成历史记录的唯一键：优先使用 tmdb_id；剧集带上季号（默认 0），避免不同季混淆
        """
        if not mediainfo or not mediainfo.tmdb_id:
            return None
        if mediainfo.type == MediaType.TV:
            season = meta.begin_season if meta.begin_season is not None else 0
            return f"{mediainfo.tmdb_id}_S{str(season).zfill(2)}"
        return str(mediainfo.tmdb_id)

    @staticmethod
    def _get_log_title(mediainfo: Dict[str, Any], meta: Dict[str, Any]) -> str:
        """
        生成标准化的日志标题
        """
        title = mediainfo.get('title_year', '') if isinstance(mediainfo, dict) else mediainfo.title_year
        m_type = mediainfo.get('type') if isinstance(mediainfo, dict) else mediainfo.type.value
        season = meta.get('season') if isinstance(meta, dict) else meta.begin_season

        if m_type == MediaType.TV.value and season is not None:
            title += f" S{str(season).zfill(2)}"
        return title

    @staticmethod
    def __is_number_or_range(value):
        return bool(re.match(r"^\d+(\.\d+)?(-\d+(\.\d+)?)?$", value))

    def get_rule_groups(self) -> List[Dict[str, Any]]:
        rule_groups: List[dict] = self.systemconfig.get(SystemConfigKey.UserFilterRuleGroups)
        if not rule_groups:
            return []
        return [{'title': group.get('name'), 'value': group.get('name')} for group in rule_groups]

    def get_downloader_for_select(self) -> List[Dict[str, Any]]:
        from app.helper.service import ServiceConfigHelper
        downloaders = ServiceConfigHelper.get_downloader_configs()
        return [{'title': d.name, 'value': d.name} for d in downloaders]