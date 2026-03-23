from scheduler import check_all_trackers


def run_all_trackers():
    """
    Legacy CLI entrypoint.
    Uses the same logic as the background scheduler so behavior stays consistent.
    """
    check_all_trackers()
