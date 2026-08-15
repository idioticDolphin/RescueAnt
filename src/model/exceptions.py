class ConfigError(Exception):
    pass

class CategoryNotFoundError(Exception):
    def __init__(self, category_name):
        print(f"Category {category_name} not found")
