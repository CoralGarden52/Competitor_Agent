from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.core.models import QAResult, RunState


class GraphExecState(TypedDict):
    run_state: RunState
    qa_output: dict[str, Any] | None
    route_action: str


class WorkflowLangGraphRuntime:
    def __init__(self, service: Any):
        self.service = service
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(GraphExecState)
        graph.add_node('plan', self._node_plan)
        graph.add_node('collect', self._node_collect)
        graph.add_node('normalize', self._node_normalize)
        graph.add_node('analyze', self._node_analyze)
        graph.add_node('draft', self._node_draft)
        graph.add_node('qa', self._node_qa)
        graph.add_node('finalize', self._node_finalize)

        graph.set_entry_point('plan')
        graph.add_edge('plan', 'collect')
        graph.add_edge('collect', 'normalize')
        graph.add_edge('normalize', 'analyze')
        graph.add_edge('analyze', 'draft')
        graph.add_edge('draft', 'qa')
        graph.add_conditional_edges(
            'qa',
            self._route_after_qa,
            {
                'finalize': 'finalize',
                'rework_collect': 'collect',
                'rework_analyze': 'analyze',
                'rework_draft': 'draft',
                'fail': END,
            },
        )
        graph.add_edge('finalize', END)
        return graph.compile()

    def execute(self, run_state: RunState) -> RunState:
        state: GraphExecState = {'run_state': run_state, 'qa_output': None, 'route_action': ''}
        result = self.graph.invoke(state)
        return result['run_state']

    def _trace(self, node_name: str, run_state: RunState, fn):
        import time
        start_time = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] START: {node_name} (run_id={run_state.run_id[:8]})")
        
        trace_id = self.service.store.trace_node_started(run_id=run_state.run_id, node_name=node_name, attempt=run_state.attempt)
        self.service.store.trace_node_input(
            run_id=run_state.run_id,
            node_name=node_name,
            input_payload={
                'industry': run_state.industry,
                'competitors': run_state.competitors,
                'user_prompt': run_state.user_prompt,
                'planned_competitors': run_state.planned_competitors,
                'status': run_state.status,
            },
        )
        try:
            fn(run_state)
            elapsed = time.time() - start_time
            print(f"[{time.strftime('%H:%M:%S')}] END:   {node_name} (耗时={elapsed:.2f}s, evidences={len(run_state.evidences)}, profiles={len(run_state.profiles)})")
            
            self.service.store.trace_node_completed(
                trace_id=trace_id,
                run_id=run_state.run_id,
                node_name=node_name,
                output_payload={
                    'status': run_state.status,
                    'evidence_count': len(run_state.evidences),
                    'profile_count': len(run_state.profiles),
                    'finding_count': len(run_state.findings),
                    'has_report': run_state.report is not None,
                },
            )
            self.service.store.save_checkpoint(
                run_id=run_state.run_id,
                node_name=node_name,
                attempt=run_state.attempt,
                state=run_state,
            )
        except Exception as exc:
            elapsed = time.time() - start_time
            print(f"[{time.strftime('%H:%M:%S')}] FAIL:  {node_name} (耗时={elapsed:.2f}s, error={str(exc)})")
            self.service.store.trace_node_failed(trace_id=trace_id, error_text=str(exc))
            raise

    def _node_plan(self, state: GraphExecState) -> GraphExecState:
        run_state = state['run_state']
        self._trace('plan', run_state, self.service._plan)
        return {'run_state': run_state, 'qa_output': None, 'route_action': ''}

    def _node_collect(self, state: GraphExecState) -> GraphExecState:
        run_state = state['run_state']
        self._trace('collect', run_state, self.service._collect)
        return {'run_state': run_state, 'qa_output': None, 'route_action': ''}

    def _node_normalize(self, state: GraphExecState) -> GraphExecState:
        run_state = state['run_state']
        self._trace('normalize', run_state, self.service._normalize)
        return {'run_state': run_state, 'qa_output': None, 'route_action': ''}

    def _node_analyze(self, state: GraphExecState) -> GraphExecState:
        run_state = state['run_state']
        self._trace('analyze', run_state, self.service._analyze)
        return {'run_state': run_state, 'qa_output': None, 'route_action': ''}

    def _node_draft(self, state: GraphExecState) -> GraphExecState:
        run_state = state['run_state']
        self._trace('draft', run_state, self.service._draft)
        return {'run_state': run_state, 'qa_output': None, 'route_action': ''}

    def _node_qa(self, state: GraphExecState) -> GraphExecState:
        run_state = state['run_state']

        def run_qa(s: RunState):
            qa = self.service._qa(s)
            state['qa_output'] = qa.model_dump()
            decision = self.service.orchestrator.route(qa_result=qa, iteration=s.attempt)
            if decision.action == 'retry':
                s.attempt += 1
                qa_result = QAResult(passed=qa.passed, issues=qa.issues, target_agent=qa.target_agent)
                self.service._apply_rework_ticket(s, qa_result)
                if decision.route_back_stage is not None:
                    if decision.route_back_stage.value == 'collect':
                        state['route_action'] = 'rework_collect'
                    elif decision.route_back_stage.value == 'analyze':
                        state['route_action'] = 'rework_analyze'
                    else:
                        state['route_action'] = 'rework_draft'
                else:
                    state['route_action'] = 'rework_draft'
            elif decision.action == 'fail':
                s.status = 'failed'
                state['route_action'] = 'fail'
            else:
                state['route_action'] = 'finalize'

        self._trace('qa', run_state, run_qa)
        return {'run_state': run_state, 'qa_output': state.get('qa_output'), 'route_action': state.get('route_action', 'finalize')}

    def _node_finalize(self, state: GraphExecState) -> GraphExecState:
        run_state = state['run_state']

        def finalize(s: RunState):
            self.service._finalize(s)
            s.status = 'completed'
            self.service.store.save_state(s)

        self._trace('finalize', run_state, finalize)
        return {'run_state': run_state, 'qa_output': state.get('qa_output'), 'route_action': 'finalize'}

    @staticmethod
    def _route_after_qa(state: GraphExecState) -> str:
        return state.get('route_action', 'finalize')
