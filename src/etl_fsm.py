#!/usr/bin/env python3
"""
FSM (Finite State Machine) Extraction from Verilog RTL

This module detects and extracts state machines from Verilog code, including:
- State machine identification
- State extraction with encodings
- Transition extraction with conditions
- Integration with existing RTL schema (modules, signals, logic chunks)

Detection Strategy:
1. Find state registers (reg [...] state, current_state, etc.)
2. Find parameter-defined state encodings
3. Find case/if statements that update state
4. Extract transitions and conditions
"""

import os
import re
import json
import hashlib
from typing import List, Dict, Tuple, Optional
from utils import NodeResolver, sanitize_id, get_edge_key, VerilogParser


class FSMExtractor:
    """Extract finite state machines from Verilog modules"""
    
    # Patterns for state machine detection
    STATE_REG_PATTERN = re.compile(
        r'^\s*reg\s+(?:\[([^\]]+)\])?\s*(\w*state\w*)\s*;',
        re.MULTILINE | re.IGNORECASE
    )
    
    PARAMETER_PATTERN = re.compile(
        r'^\s*parameter\s+(\w+)\s*=\s*([^;]+);',
        re.MULTILINE
    )
    
    LOCALPARAM_PATTERN = re.compile(
        r'^\s*localparam\s+(\w+)\s*=\s*([^;]+);',
        re.MULTILINE
    )
    
    DEFINE_PATTERN = re.compile(
        r'^\s*`define\s+(\w+)\s+([^\n]+)',
        re.MULTILINE
    )
    
    # Pattern for case statements (FSM logic)
    CASE_PATTERN = re.compile(
        r'case\s*\(([^\)]+)\)(.*?)endcase',
        re.DOTALL | re.MULTILINE
    )
    
    # Pattern for case items
    CASE_ITEM_PATTERN = re.compile(
        r'^\s*([^:]+):\s*(.*?)(?=^\s*\w+:|^\s*default:|^\s*endcase)',
        re.MULTILINE | re.DOTALL
    )
    
    def __init__(self, module_name: str, module_body: str, module_key: str, 
                 file_content: str = None, resolver: NodeResolver = None):
        self.module_name = module_name
        self.module_body = module_body
        self.module_key = module_key
        self.file_content = file_content or module_body  # Full file for `define` lookup
        self.resolver = resolver
        self.fsms = []
        self.states = []
        self.transitions = []
        self.edges = []
        
        # Extract parameter definitions from both module and file
        self.parameters = self._extract_parameters()
        
    def _extract_parameters(self) -> Dict[str, str]:
        """Extract parameter definitions from module and file"""
        params = {}
        
        # Extract regular parameters from module
        for match in self.PARAMETER_PATTERN.finditer(self.module_body):
            param_name = match.group(1)
            param_value = match.group(2).strip()
            params[param_name] = param_value
        
        # Extract localparams from module
        for match in self.LOCALPARAM_PATTERN.finditer(self.module_body):
            param_name = match.group(1)
            param_value = match.group(2).strip()
            params[param_name] = param_value
        
        # Extract `define macros from FULL FILE (they're often at top)
        for match in self.DEFINE_PATTERN.finditer(self.file_content):
            param_name = match.group(1)
            param_value = match.group(2).strip()
            params[param_name] = param_value
        
        return params
    
    def _identify_state_encodings(self, state_reg_name: str) -> Dict[str, str]:
        """Identify state encodings from parameters
        
        Returns dict of {STATE_NAME: encoding_value}
        """
        encodings = {}
        
        # Look for parameters that look like state names
        # Common patterns: IDLE, S_IDLE, STATE_IDLE, FETCH, etc.
        state_keywords = ['IDLE', 'RESET', 'FETCH', 'DECODE', 'EXECUTE', 'WRITE',
                         'READ', 'WAIT', 'DONE', 'INIT', 'START', 'STOP', 'ERROR',
                         'LOOP', 'FLUSH', 'INV', 'LOAD', 'STORE', 'CLOAD']
        
        # Also look for FSM-specific prefixes (e.g., DCFSM, ICFSM, IFFSM)
        fsm_prefixes = []
        for param_name in self.parameters.keys():
            # Extract FSM prefix patterns like OR1200_DCFSM, OR1200_ICFSM
            if 'FSM' in param_name.upper():
                # Extract the prefix before the actual state name
                parts = param_name.split('_')
                for i, part in enumerate(parts):
                    if 'FSM' in part.upper() and i < len(parts) - 1:
                        prefix = '_'.join(parts[:i+1])
                        if prefix not in fsm_prefixes:
                            fsm_prefixes.append(prefix)
        
        for param_name, param_value in self.parameters.items():
            # Check if parameter name contains state keywords
            param_upper = param_name.upper()
            
            # Is this likely a state definition?
            is_state = False
            
            # Method 1: Contains state keyword
            if any(keyword in param_upper for keyword in state_keywords):
                is_state = True
            
            # Method 2: Starts with S_ or STATE_
            if param_upper.startswith('S_') or param_upper.startswith('STATE_'):
                is_state = True
            
            # Method 3: Matches FSM prefix pattern (e.g., OR1200_DCFSM_*)
            # Excludes structural suffixes (WIDTH is the state register's own
            # bit-width, e.g. `define OR1200_EXCEPTFSM_WIDTH 3 -- it always
            # self-matches here since it's what *defines* the prefix being
            # tested, not an encoded state value).
            NON_STATE_SUFFIXES = {'WIDTH', 'SIZE', 'DEPTH'}
            for prefix in fsm_prefixes:
                if param_name.startswith(prefix + '_'):
                    suffix = param_name[len(prefix) + 1:].upper()
                    if suffix not in NON_STATE_SUFFIXES:
                        is_state = True
                    break
            
            # Method 4: Value looks like a state encoding (e.g., 3'b000, 4'd0)
            if re.match(r"^\d+'[bodh]\d+$", param_value.strip()):
                # If it also has a numeric component, likely a state
                if re.search(r'\d+$', param_name):
                    is_state = True
            
            if is_state:
                encodings[param_name] = param_value.strip()
        
        return encodings
    
    def _extract_transitions_from_case(self, case_expr: str, case_body: str, 
                                       state_reg_name: str) -> List[Dict]:
        """Extract state transitions from case statement"""
        transitions = []
        
        # Find all case items
        case_items = []
        current_pos = 0
        
        for match in re.finditer(r'^\s*([^:]+):', case_body, re.MULTILINE):
            state_value = match.group(1).strip()
            start = match.end()
            
            # Find end of this case item (next case item or endcase)
            next_match = re.search(r'^\s*(\w+:|`\w+:|default:|endcase)', 
                                  case_body[start:], re.MULTILINE)
            if next_match:
                end = start + next_match.start()
            else:
                end = len(case_body)
            
            case_item_body = case_body[start:end]
            case_items.append((state_value, case_item_body))
        
        # Extract transitions from each case item
        for from_state, body in case_items:
            # Look for state assignments: state <= NEW_STATE or state = NEW_STATE
            # Handle both direct names and backtick references
            assign_pattern = rf'{re.escape(state_reg_name)}\s*[<]?=\s*(`?\w+)'
            
            for assign_match in re.finditer(assign_pattern, body):
                to_state = assign_match.group(1).strip()
                # Remove backtick if present
                to_state = to_state.lstrip('`')
                
                # Extract condition (if statement before assignment)
                # Look backwards for if/else if
                assign_pos = assign_match.start()
                before = body[:assign_pos]
                
                condition = None
                if_match = re.search(r'if\s*\((.*?)\)\s*begin?', before, re.DOTALL)
                if if_match:
                    condition = if_match.group(1).strip()
                elif 'else' in before[-50:]:
                    condition = "else"
                
                transitions.append({
                    'from_state': from_state,
                    'to_state': to_state,
                    'condition': condition,
                    'case_expr': case_expr
                })
        
        return transitions
    
    def extract(self) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        """Main extraction method
        
        Returns: (fsms, states, transitions)
        """
        # Step 1: Find state registers
        state_registers = []
        for match in self.STATE_REG_PATTERN.finditer(self.module_body):
            width = match.group(1)  # e.g., "2:0"
            name = match.group(2)    # e.g., "state", "current_state"
            state_registers.append({
                'name': name,
                'width': width,
                'match_pos': match.start()
            })
        
        if not state_registers:
            return [], [], []  # No FSMs found
        
        # Step 2: For each state register, try to extract FSM
        for state_reg in state_registers:
            state_reg_name = state_reg['name']
            
            # Identify state encodings
            encodings = self._identify_state_encodings(state_reg_name)
            
            if len(encodings) < 2:
                continue  # Not enough states, probably not an FSM
            
            # Find case statements that use this state register
            fsm_logic = []
            for case_match in self.CASE_PATTERN.finditer(self.module_body):
                case_expr = case_match.group(1).strip()
                case_body = case_match.group(2)
                
                # Does this case statement operate on our state register?
                if state_reg_name in case_expr:
                    fsm_logic.append({
                        'case_expr': case_expr,
                        'case_body': case_body,
                        'match_pos': case_match.start()
                    })
            
            if not fsm_logic:
                continue  # No case statement found for this state register
            
            # Identify which logic chunks implement this FSM
            # Load nodes from data directory to find always block IDs
            logic_chunks_found = set()
            if self.resolver:
                import json
                import os
                rtl_nodes_file = os.path.join(self.resolver.data_dir, 'rtl_nodes.json')
                if os.path.exists(rtl_nodes_file):
                    with open(rtl_nodes_file, 'r') as f:
                        rtl_nodes = json.load(f)
                        mod_chunks = [n for n in rtl_nodes if n['type'] == 'RTL_LogicChunk' and n['id'].startswith(self.module_name + '.')]
                        
                        # Heuristic: If the logic chunk assigns to the state register, 
                        # it's likely the implementation of the FSM.
                        state_assign_pattern = re.compile(rf'\b{re.escape(state_reg_name)}\b\s*[<]?=')

                        for chunk in mod_chunks:
                            chunk_code = chunk.get('metadata', {}).get('code', '')
                            if state_assign_pattern.search(chunk_code):
                                logic_chunks_found.add(chunk['id'])

            # Create FSM node
            fsm_id = sanitize_id(f"{self.module_name}_{state_reg_name}_fsm")
            fsm_doc = {
                '_key': fsm_id,
                'type': 'FSM_StateMachine',
                'name': f"{self.module_name}_{state_reg_name}",
                'state_register': state_reg_name,
                'state_count': len(encodings),
                'parent_module': self.module_key,
                'metadata': {
                    'width': state_reg['width'],
                    'style': 'case_based',  # Could detect mealy vs moore
                    'encoding_type': self._guess_encoding_type(encodings)
                }
            }
            self.fsms.append(fsm_doc)
            
            # IMPLEMENTED_BY: FSM → LogicChunk
            for l_id in logic_chunks_found:
                self.edges.append({
                    '_key': get_edge_key(fsm_id, l_id, 'IMPLEMENTED_BY'),
                    'from': fsm_id,
                    'to': l_id,
                    'type': 'IMPLEMENTED_BY'
                })
            
            # Create state nodes
            for state_name, encoding in encodings.items():
                state_id = sanitize_id(f"{fsm_id}_{state_name}")
                state_doc = {
                    '_key': state_id,
                    'type': 'FSM_State',
                    'name': state_name,
                    'fsm_id': fsm_id,
                    'encoding': encoding,
                    'metadata': {
                        'is_reset_state': 'IDLE' in state_name.upper() or 'RESET' in state_name.upper()
                    }
                }
                self.states.append(state_doc)
                
                # Create HAS_STATE edge
                self.edges.append({
                    '_key': get_edge_key(fsm_id, state_id, 'HAS_STATE'),
                    'from': fsm_id,
                    'to': state_id,
                    'type': 'HAS_STATE'
                })
            
            # Extract transitions
            seen_transitions = set()  # Dedup: (from, to, condition)
            
            for logic in fsm_logic:
                case_transitions = self._extract_transitions_from_case(
                    logic['case_expr'],
                    logic['case_body'],
                    state_reg_name
                )
                
                for trans in case_transitions:
                    # Map parameter names to state IDs
                    from_state_name = trans['from_state'].lstrip('`')
                    to_state_name = trans['to_state'].lstrip('`')
                    
                    # Try to resolve parameter names
                    if from_state_name in encodings:
                        from_state_id = sanitize_id(f"{fsm_id}_{from_state_name}")
                    else:
                        continue  # Unknown state
                    
                    if to_state_name in encodings:
                        to_state_id = sanitize_id(f"{fsm_id}_{to_state_name}")
                    else:
                        continue  # Unknown state
                    
                    # Check for duplicates
                    trans_sig = (from_state_id, to_state_id, trans['condition'])
                    if trans_sig in seen_transitions:
                        continue
                    seen_transitions.add(trans_sig)
                    
                    # Create transition edge
                    # Include condition in key to allow multiple transitions between same states
                    trans_key_base = f"{from_state_id}:{to_state_id}:{trans['condition'] or 'default'}"
                    trans_key = hashlib.md5(trans_key_base.encode()).hexdigest()
                    
                    self.edges.append({
                        '_key': trans_key,
                        'from': from_state_id,
                        'to': to_state_id,
                        'type': 'TRANSITIONS_TO',
                        'condition': trans['condition'],
                        'metadata': {
                            'case_expr': trans['case_expr']
                        }
                    })
            
            # Create schema integration edges
            # HAS_FSM: Module → FSM
            self.edges.append({
                '_key': get_edge_key(self.module_key, fsm_id, 'HAS_FSM'),
                'from': self.module_key,
                'to': fsm_id,
                'type': 'HAS_FSM'
            })
            
            # STATE_REGISTER: FSM → Signal
            if self.resolver:
                signal_id = self.resolver.resolve_id(self.module_name, state_reg_name)
            else:
                signal_id = sanitize_id(f"{self.module_name}.{state_reg_name}")
                
            self.edges.append({
                '_key': get_edge_key(fsm_id, signal_id, 'STATE_REGISTER'),
                'from': fsm_id,
                'to': signal_id,
                'type': 'STATE_REGISTER'
            })
        
        return self.fsms, self.states, self.edges
    
    def _guess_encoding_type(self, encodings: Dict[str, str]) -> str:
        """Guess the encoding type from state values"""
        values = list(encodings.values())
        if not values:
            return "unknown"
        
        sample = values[0]
        if "'b" in sample:
            return "binary"
        elif "'d" in sample:
            return "decimal"
        elif "'h" in sample:
            return "hexadecimal"
        elif "'o" in sample:
            return "octal"
        else:
            return "unknown"


def extract_fsms_from_modules(rtl_nodes_file: str, rtl_edges_file: str,
                               rtl_dir: str, resolver: NodeResolver = None) -> Tuple[List, List, List]:
    """Extract FSMs from all modules
    
    Args:
        rtl_nodes_file: Path to RTL nodes JSON
        rtl_edges_file: Path to RTL edges JSON
        rtl_dir: Directory containing Verilog files
    
    Returns:
        (fsm_nodes, state_nodes, fsm_edges)
    """
    print("="*60)
    print("FSM Extraction Starting...")
    print("="*60)
    
    # Load existing RTL nodes to get module list
    with open(rtl_nodes_file, 'r') as f:
        rtl_nodes = json.load(f)
    
    modules = [n for n in rtl_nodes if n['type'] == 'RTL_Module']
    print(f"\nFound {len(modules)} modules to analyze")
    
    # Read Verilog files
    file_map = {}
    for fname in os.listdir(rtl_dir):
        if fname.endswith('.v'):
            path = os.path.join(rtl_dir, fname)
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                file_map[fname] = f.read()
    
    all_fsms = []
    all_states = []
    all_edges = []
    
    # Extract module bodies using VerilogParser
    for module in modules:
        module_name = module.get('_key') or module.get('id')
        source_file = module.get('metadata', {}).get('file')
        
        if not source_file or source_file not in file_map:
            continue
        
        content = file_map[source_file]
        
        # Find this module's body using centralized parser
        for name, body in VerilogParser.get_module_bodies(content):
            if name == module_name:
                # Extract FSMs - pass both module body AND full file content
                extractor = FSMExtractor(module_name, body, module_name, content, resolver=resolver)
                fsms, states, edges = extractor.extract()
                
                if fsms:
                    print(f"\n  [{module_name}]")
                    print(f"    FSMs: {len(fsms)}")
                    print(f"    States: {len(states)}")
                    print(f"    Transitions: {len([e for e in edges if e['type'] == 'TRANSITIONS_TO'])}")
                    
                    all_fsms.extend(fsms)
                    all_states.extend(states)
                    all_edges.extend(edges)
                
                break
    
    print(f"\n{'='*60}")
    print(f"FSM Extraction Complete")
    print(f"  Total FSMs: {len(all_fsms)}")
    print(f"  Total States: {len(all_states)}")
    print(f"  Total Edges: {len(all_edges)}")
    print(f"{'='*60}\n")
    
    return all_fsms, all_states, all_edges


if __name__ == "__main__":
    # For testing
    from config import RTL_NODES_FILE, RTL_EDGES_FILE, RTL_DIR, DATA_DIR
    
    resolver = NodeResolver(DATA_DIR)
    fsms, states, edges = extract_fsms_from_modules(
        RTL_NODES_FILE, 
        RTL_EDGES_FILE,
        RTL_DIR,
        resolver=resolver
    )
    
    # Write output files
    with open(os.path.join(DATA_DIR, 'fsm_nodes.json'), 'w') as f:
        json.dump(fsms, f, indent=2)
    
    with open(os.path.join(DATA_DIR, 'fsm_state_nodes.json'), 'w') as f:
        json.dump(states, f, indent=2)
    
    with open(os.path.join(DATA_DIR, 'fsm_edges.json'), 'w') as f:
        json.dump(edges, f, indent=2)
    
    print(f"Output written to:")
    print(f"  - data/fsm_nodes.json")
    print(f"  - data/fsm_state_nodes.json")
    print(f"  - data/fsm_edges.json")

