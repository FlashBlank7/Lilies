def main(inputs):
    """Build additional requests from server pagination metadata; never drop later pages."""
    plan=[]
    for page in inputs['first_pages']:
        pages=int(page['headers']['x-test-pages'])
        if not 1<=pages<=100:
            raise ValueError('unreasonable page count')
        for n in range(2,pages+1):
            plan.append({'service':page['service'],'url':page['url'],'page':n})
    return {'requests':plan}
