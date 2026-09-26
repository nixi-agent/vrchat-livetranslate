"""VRChat 实时同传 —— 核心包。

层次只有三层：采集(capture) → 会话(session) → 输出(output)。
适配不同代次模型的所有差异都收敛在 session/ 内部，下游永远看不到模型细节。
"""

__version__ = "0.0.4"
