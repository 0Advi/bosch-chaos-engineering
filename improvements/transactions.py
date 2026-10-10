"""Backend-neutral image transaction with rollback; real adapter belongs to P2.

The injected operator must enforce persistent locking and supervise crash recovery.
These steps are tested with a fake operator and do not claim Docker verification.
"""
from contextlib import AbstractContextManager
from typing import Callable, Protocol

from controller.schemas.contracts import SandboxRef


class ImageOperator(Protocol):
    def exclusive_lock(self) -> AbstractContextManager: ...
    def verify_idle_recovered_sandbox(self, sandbox: SandboxRef) -> None: ...
    def current_images(self, services: list[str]) -> dict[str, str]: ...
    def replace_images(self, images: dict[str, str]) -> None: ...
    def verify_health(self, services: list[str], timeout_seconds: int) -> bool: ...
    def block_new_experiments(self) -> None: ...


class TransactionalBackend:
    def __init__(self, operator: ImageOperator, journal: Callable[[dict], None], timeout_seconds=30):
        if not isinstance(timeout_seconds,int) or isinstance(timeout_seconds,bool) or not 1 <= timeout_seconds <= 60:
            raise ValueError('health deadline must be 1–60 seconds')
        self.operator=operator
        self.journal=journal
        self.timeout=timeout_seconds

    def deploy_transaction(self, images, rollback_images, sandbox):
        services=sorted(images)
        if not services or set(images)!=set(rollback_images):
            raise ValueError('image sets must match')
        with self.operator.exclusive_lock():
            self.operator.verify_idle_recovered_sandbox(sandbox)
            prior=self.operator.current_images(services)
            if prior!=rollback_images:
                raise ValueError('approved rollback images no longer match the sandbox')
            base={'sandbox':sandbox.model_dump(),'old_images':prior,'new_images':images}
            # Persist recovery intent before the first service change.
            self.journal({**base,'outcome':'deploying'})
            try:
                self.operator.replace_images(images)
                if self.operator.verify_health(services,self.timeout) is not True:
                    raise RuntimeError('new application failed health verification')
                result={**base,'outcome':'verified','rollback_verified':False}
                self.journal(result)
                return result
            except Exception:
                try:
                    self.operator.replace_images(prior)
                    if self.operator.verify_health(services,self.timeout) is not True:
                        raise RuntimeError('rollback health unverified')
                    result={**base,'outcome':'rolled_back','rollback_verified':True}
                    self.journal(result)
                    return result
                except Exception:
                    self.operator.block_new_experiments()
                    result={**base,'outcome':'needs_attention','rollback_verified':False}
                    # If persistence also fails, propagate the failure; never report success.
                    self.journal(result)
                    return result
