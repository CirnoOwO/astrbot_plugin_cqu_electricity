import importlib.util
import sys
from pathlib import Path

ROOT=Path(__file__).parent
spec=importlib.util.spec_from_file_location("cqu_plugin",ROOT/"__init__.py",submodule_search_locations=[str(ROOT)])
module=importlib.util.module_from_spec(spec)
sys.modules["cqu_plugin"]=module
spec.loader.exec_module(module)

# Local contract tests use a small API stub. The same tests run against the real
# installed AstrBot API in a separate process on the deployment host.
if importlib.util.find_spec("astrbot") is None:
    import types
    from types import SimpleNamespace
    for name in ("astrbot","astrbot.api","astrbot.api.event","astrbot.api.star","astrbot.core","astrbot.core.utils","astrbot.core.utils.astrbot_path","astrbot.core.message","astrbot.core.message.components","astrbot.core.message.message_event_result"):
        value=types.ModuleType(name);value.__path__=[];sys.modules[name]=value
    def decorator(*args,**kwargs):return lambda fn:fn
    api=sys.modules["astrbot.api"]
    api.AstrBotConfig=dict
    api.logger=SimpleNamespace(info=lambda *x:None,warning=lambda *x:None)
    event=sys.modules["astrbot.api.event"]
    event.AstrMessageEvent=object
    event.filter=SimpleNamespace(command=decorator,llm_tool=decorator)
    class Star:
        def __init__(self,context):self.context=context
    star=sys.modules["astrbot.api.star"]
    star.Context=object;star.Star=Star;star.register=decorator
    sys.modules["astrbot.core.utils.astrbot_path"].get_astrbot_data_path=lambda:"."
    sys.modules["astrbot.core.message.message_event_result"].MessageChain=lambda components:components
    sys.modules["astrbot.core.message.components"].Plain=lambda text:text
    sys.modules["astrbot.core.message.components"].At=lambda **kwargs:kwargs
    sys.modules["astrbot.core.message.components"].Image=SimpleNamespace(fromFileSystem=lambda path:SimpleNamespace(file=path))

    for name in ("astrbot.core.pipeline","astrbot.core.pipeline.whitelist_check","astrbot.core.pipeline.whitelist_check.stage"):
        value=types.ModuleType(name);value.__path__=[];sys.modules[name]=value
    class WhitelistCheckStage:
        async def process(self,event):
            if event.unified_msg_origin not in self.whitelist and str(event.get_group_id()) not in self.whitelist:
                event.stop_event()
    sys.modules["astrbot.core.pipeline.whitelist_check.stage"].WhitelistCheckStage=WhitelistCheckStage
