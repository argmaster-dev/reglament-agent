class ModelError(Exception):
    pass


class QueueFull(ModelError):
    """Generation queue is at MODEL_CHAT_QUEUE_MAX; render as "стенд занят, повторите"."""
