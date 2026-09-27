import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def central_check():
    from central.services import mark_offline_nodes

    try:
        mark_offline_nodes()
    except Exception as e:
        logger.warning("central_check error: %s", e)
