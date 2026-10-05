from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='建筑抗震鉴定与加固排序'; ENTITY='抗震鉴定'; BATCH_ENTITY='复评批次'; ID_PREFIX='SR'
SEVERITIES=['low', 'medium', 'high', 'severe']; STATES=['proposed', 'assessed', 'design', 'construction', 'accepted', 'rejected']; TRANSITIONS={'proposed': ['assessed'], 'assessed': ['design', 'rejected'], 'design': ['construction'], 'construction': ['accepted'], 'accepted': ['rejected'], 'rejected': []}; TRANSITION_ROLES={'assessed': ['assessor'], 'design': ['structural_engineer'], 'construction': ['structural_engineer'], 'accepted': ['review_board'], 'rejected': ['review_board']}
CREATE_ROLES=set(['assessor']); RECORD_ROLES=set(['assessor', 'structural_engineer']); AUDIT_ROLES=set(['review_board', 'viewer']); VIEW_ROLES=set(['assessor', 'structural_engineer', 'review_board', 'viewer'])
# 震后复评批次：现场队(assessor)与结构工程师可提交批次；待复核版本由工程师/评审委员会提为生效；审核结论由评审委员会作出
BATCH_ROLES=set(['assessor','structural_engineer']); PROMOTE_ROLES=set(['structural_engineer','review_board']); REVIEW_DECIDE_ROLES=set(['review_board']); BACKFILL_ROLES=set(['assessor'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'severe': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'severe': 4}; TERMINAL_STATES=set(['accepted', 'rejected'])
VERSION_STATUSES=('effective','pending_review','superseded'); SCHEME_STATUSES=('draft','effective','superseded','invalidated'); CONCLUSION_KINDS=('priority','review'); CONCLUSION_STATUSES=('active','pending','invalidated','rejected')
# 常见材料牌号的标称强度，未显式给定期望值时作为测量比对基准
MATERIAL_EXPECTED={'C25':25.0,'C30':30.0,'C35':35.0,'C40':40.0,'C50':50.0,'Q235':235.0,'Q355':355.0,'HRB400':400.0}
def expected_strength(material,declared=None):
    if declared is not None and declared>0: return float(declared)
    return MATERIAL_EXPECTED.get(str(material).upper())
def measurement_ratio(measurements):
    ratios=[]
    for m in measurements:
        expected=expected_strength(m.get('material'),m.get('expected_value'))
        value=m.get('measured_value')
        if expected and value is not None: ratios.append(float(value)/float(expected))
    return min(ratios) if ratios else 1.0
def occupant_factor(density):
    try: density=max(0.0,float(density))
    except (TypeError,ValueError): return 0.0
    return min(3.0,density*3.0)
def measurement_penalty(ratio):
    try: ratio=max(0.0,min(1.0,float(ratio)))
    except (TypeError,ValueError): return 0.0
    return min(4.0,(1.0-ratio)*4.0)
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0,occupant_density=0.0,measurement_ratio=1.0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    score=SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))+occupant_factor(occupant_density)+measurement_penalty(measurement_ratio)
    return max(0,min(10,int(round(score))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
