class ConfigError(Exception):
    """Raised when bot.config is missing a required key or fails to parse."""
    pass

class CategoryNotFoundError(Exception):
    """Raised when a category name doesn't match any configured Category."""
    def __init__(self, category_name):
        super().__init__(f"Category '{category_name}' not found")
