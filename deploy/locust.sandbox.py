"""Reuse upstream shop traffic without requests to the excluded shopping agent."""
import locustfile as upstream

WebsiteUser = upstream.WebsiteUser
WebsiteUser.tasks = [task for task in WebsiteUser.tasks if task is not WebsiteUser.ask_agent]

if hasattr(upstream, "WebsiteBrowserUser"):
    WebsiteBrowserUser = upstream.WebsiteBrowserUser
