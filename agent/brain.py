import os
from dataclasses import dataclass, field

@dataclass
class Orchestrator:
    delegators = []
    indexes = []
    def __init__(self, agg_function, *elements):
        self.agg_function = agg_function
        for e in elements:
            self.e = e
    
    # --> 

    
    def __repr__(self, e, n):
        return '{self.e} using trigger {n}'
    
    def usage_function(self, unit, indexes):
        for element in indexes():
            nw_num = int(element[unit])
            nw_nxs = zip((nw_num, 1), (0, nw_num))
