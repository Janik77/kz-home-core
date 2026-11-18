class EntityNotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class SceneDraftError(ConflictError):
    def __init__(self):
        super().__init__("Scene is a draft; activate it explicitly before execution")


class InvalidReferenceError(Exception):
    pass


class UnavailableError(Exception):
    pass


class DefinitionRevisionConflict(ConflictError):
    def __init__(self):
        super().__init__("Definition changed; reload before saving")


class MetadataRevisionConflict(ConflictError):
    def __init__(self):
        super().__init__("Metadata changed; reload before saving")
