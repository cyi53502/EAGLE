from eagle.domain.enums import CandidateState, CandidateType


class CommitmentGate:
    def decide(self, candidate) -> CandidateState:
        if candidate.candidate_type == CandidateType.PREFERENCE.value:
            return self._decide_preference(candidate)
        return self._decide_knowledge(candidate)

    @staticmethod
    def _decide_preference(candidate) -> CandidateState:
        if candidate.has_unresolved_conflict:
            return CandidateState.DEFER
        if candidate.explicit_user_statement:
            return CandidateState.COMMITTED
        if candidate.independent_choices >= 3 and candidate.distinct_sessions >= 2 and candidate.negative_evidence == 0:
            return CandidateState.COMMITTED
        return CandidateState.PENDING

    @staticmethod
    def _decide_knowledge(candidate) -> CandidateState:
        if candidate.has_unresolved_conflict:
            return CandidateState.DEFER
        if candidate.user_confirmed or candidate.same_condition_success >= 2:
            return CandidateState.COMMITTED
        return CandidateState.PENDING
