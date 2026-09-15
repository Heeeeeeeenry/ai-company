"""ai-company 的 Django 可插拔嵌入件。

把本目录（包名 ai_company）整体拷进宿主 Django 项目根（与 manage.py 同级），
或放进任意已在 PYTHONPATH 的目录，然后按 README.md 三步接入：

    1) INSTALLED_APPS += ["ai_company"]
    2) urls.py: path("ai/", include("ai_company.urls"))
    3) settings.py: AI_COMPANY_BASE_URL = "http://127.0.0.1:8020"

AppConfig 由 Django 自动发现（apps.py），无需在此声明。
"""
