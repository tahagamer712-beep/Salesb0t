"""أدوات مشتركة بين bot.py و admin.py — ملف منفصل لتفادي circular imports."""
import asyncio
import functools


async def run_blocking(func, *args, **kwargs):
    """يشغّل دالة متزامنة (توليد إكسل، استعلامات DB ثقيلة...) بـ thread منفصل
    بدل ما تجمّد event loop الأساسي — أي عملية متزامنة داخل async handler توقف
    البوت كامل لكل المستخدمين لين تخلص، مو بس الشخص اللي طلبها."""
    loop = asyncio.get_running_loop()
    if kwargs:
        func = functools.partial(func, *args, **kwargs)
        return await loop.run_in_executor(None, func)
    return await loop.run_in_executor(None, func, *args)
