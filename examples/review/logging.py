def f():
    try:
        work()
    except ValueError:
        logger.error('failed')
