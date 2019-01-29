# -*- coding: utf8 -*-
"""
Agent for hierarchical reinforcement learning. The master agent first generates a goal, and the goal will be inputted
into the lower agent.
"""

import numpy as np
import copy
import sys, os
import random
from collections import namedtuple
from collections import deque
sys.path.append(os.getcwd().replace("src/dialogue_system/agent",""))
from src.dialogue_system.agent.agent_dqn import AgentDQN as LowerAgent
from src.dialogue_system.policy_learning.dqn_torch import DQN
from src.dialogue_system.agent.utils import state_to_representation_last
from src.dialogue_system import dialogue_configuration
from src.dialogue_system.policy_learning.internal_critic import InternalCritic

random.seed(12345)


class AgentWithGoal(object):
    def __init__(self, action_set, slot_set, disease_symptom, parameter):
        self.action_set = action_set
        self.slot_set = slot_set
        self.disease_symptom = disease_symptom

        ##################
        # Master policy.
        #######################
        input_size = parameter.get("input_size_dqn")
        hidden_size = parameter.get("hidden_size_dqn", 100)
        self.output_size = parameter.get('goal_dim', 4)
        self.dqn = DQN(input_size=input_size,
                       hidden_size=hidden_size,
                       output_size=self.output_size,
                       parameter=parameter,
                       named_tuple=('state', 'agent_action', 'reward', 'next_state', 'episode_over'))
        self.parameter = parameter
        self.experience_replay_pool = deque(maxlen=parameter.get("experience_replay_pool_size"))

        ###############################
        # Internal critic
        ##############################
        # symptom distribution by diseases.
        temp_slot_set = copy.deepcopy(slot_set)
        temp_slot_set.pop('disease')
        self.disease_to_symptom_dist = {}
        self.id2disease = {}
        total_count = np.zeros(len(temp_slot_set))
        for disease, v in self.disease_symptom.items():
            dist = np.zeros(len(temp_slot_set))
            self.id2disease[v['index']] = disease
            for symptom, count in v['symptom'].items():
                dist[temp_slot_set[symptom]] = count
                total_count[temp_slot_set[symptom]] += count
            self.disease_to_symptom_dist[disease] = dist

        for disease in self.disease_to_symptom_dist.keys():
            self.disease_to_symptom_dist[disease] = self.disease_to_symptom_dist[disease] / total_count
        goal_embed_value = [0] * len(disease_symptom)
        for disease in self.disease_to_symptom_dist.keys():
            self.disease_to_symptom_dist[disease] = self.disease_to_symptom_dist[disease] / total_count
            goal_embed_value[disease_symptom[disease]['index']] = list(self.disease_to_symptom_dist[disease])

        self.internal_critic = InternalCritic(input_size=len(temp_slot_set)*3 + self.output_size, hidden_size=50,
                                              output_size=len(temp_slot_set), goal_num=self.output_size,
                                              goal_embedding_value=goal_embed_value, slot_set=temp_slot_set,
                                              parameter=parameter)
        print(os.getcwd())
        self.internal_critic.restore_model('../agent/pre_trained_internal_critic_dropout.pkl')

        #################
        # Lower agent.
        ##############
        temp_parameter = copy.deepcopy(parameter)
        temp_parameter['input_size_dqn'] = input_size + self.output_size
        self.lower_agent = LowerAgent(action_set=action_set, slot_set=slot_set, disease_symptom=disease_symptom, parameter=temp_parameter,disease_as_action=False)
        named_tuple = ('state', 'agent_action', 'reward', 'next_state', 'episode_over','goal')
        self.lower_agent.dqn.Transition = namedtuple('Transition', named_tuple)
        self.visitation_count = np.zeros(shape=(self.output_size, len(self.lower_agent.action_space))) # [goal_num, lower_action_num]

    def initialize(self):
        """
        Initializing an dialogue session.
        :return: nothing to return.
        """
        self.master_reward = 0.
        self.sub_task_terminal = True
        self.master_action_index = None
        self.intrinsic_reward = 0.0
        self.sub_task_turn = 0
        self.lower_agent.initialize()
        self.action = {'action': 'inform',
                       'inform_slots': {"disease": 'UNK'},
                        'request_slots': {},
                        "explicit_inform_slots": {},
                        "implicit_inform_slots": {}}

    def next(self, state, turn, greedy_strategy, **kwargs):
        """
        The master first select a goal, then the lower agent takes an action based on this goal and state.
        :param state: a vector, the representation of current dialogue state.
        :param turn: int, the time step of current dialogue session.
        :return: the agent action, a tuple consists of the selected agent action and action index.
        """

        if self.sub_task_terminal is True:
            self.master_reward = 0.0
            self.master_state = state
            self.sub_task_turn = 0
            self.master_action_index = self.__master_next__(state, greedy_strategy)
        else:
            pass

        # intrinsic critic. Informing disease or not ?
        _, self.inform_disease, _ = self.intrinsic_critic(state, self.master_action_index)

        # Both the sub-task and session is terminated.
        if self.inform_disease is True:
            self.action["turn"] = turn
            self.action["inform_slots"] = {"disease":self.id2disease[self.master_action_index]}
            self.action["speaker"] = 'agent'
            self.action["action_index"] = None
            return self.action, None

        # Lower agent takes an agent. Not inform disease.
        goal = np.zeros(self.output_size)
        self.sub_task_turn += 1
        goal[self.master_action_index] = 1
        agent_action, action_index = self.lower_agent.next(state, turn, greedy_strategy, goal=goal)

        # print(self.master_action_index, self.sub_task_terminal)
        return agent_action, action_index

    def __master_next__(self, state, greedy_strategy):
        # disease_symptom are not used in state_rep.
        epsilon = self.parameter.get("epsilon")
        state_rep = state_to_representation_last(state=state,
                                                 action_set=self.action_set,
                                                 slot_set=self.slot_set,
                                                 disease_symptom=self.disease_symptom,
                                                 max_turn=self.parameter["max_turn"])  # sequence representation.
        # Master agent takes an action, i.e., selects a goal.
        if greedy_strategy == True:
            greedy = random.random()
            if greedy < epsilon:
                master_action_index = random.randint(0, self.output_size - 1)
            else:
                master_action_index = self.dqn.predict(Xs=[state_rep])[1]
        # Evaluating mode.
        else:
            master_action_index = self.dqn.predict(Xs=[state_rep])[1]
        return master_action_index

    def train(self, batch):
        """
        Training the agent.
        Args:
            batch: the sam ple used to training.
        Return:
             dict with a key `loss` whose value it a float.
        """
        loss = self.dqn.singleBatch(batch=batch,params=self.parameter,weight_correction=self.parameter.get("weight_correction"))
        return loss

    def update_target_network(self):
        self.dqn.update_target_network()
        self.lower_agent.update_target_network()

    def save_model(self, model_performance, episodes_index, checkpoint_path=None):
        # Saving master agent
        self.dqn.save_model(model_performance=model_performance, episodes_index=episodes_index, checkpoint_path=checkpoint_path)
        # Saving lower agent
        temp_checkpoint_path = os.path.join(checkpoint_path, 'lower/')
        self.lower_agent.dqn.save_model(model_performance=model_performance, episodes_index=episodes_index, checkpoint_path=temp_checkpoint_path)

    def train_dqn(self):
        """
        Train dqn.
        :return:
        """
        # ('state', 'agent_action', 'reward', 'next_state', 'episode_over')
        # Training of master agent
        cur_bellman_err = 0.0
        batch_size = self.parameter.get("batch_size",16)
        for iter in range(int(len(self.experience_replay_pool) / (batch_size))):
            batch = random.sample(self.experience_replay_pool, batch_size)
            loss = self.train(batch=batch)
            cur_bellman_err += loss["loss"]
        print("[Master agent] cur bellman err %.4f, experience replay pool %s" % (float(cur_bellman_err) / (len(self.experience_replay_pool) + 1e-10), len(self.experience_replay_pool)))
        # Training of lower agents.
        self.lower_agent.train_dqn()

    def record_training_sample(self, state, agent_action, reward, next_state, episode_over):
        """
        这里lower agent和master agent的sample都是在这里直接保存的，没有再另外调用函数。
        """
        # reward shaping
        alpha = self.parameter.get("weight_for_reward_shaping")
        # if episode_over is True: shaping = self.reward_shaping(agent_action, self.master_action_index)
        # else: shaping = 0
        shaping = 0
        reward = reward + alpha * shaping
        # state to vec.
        state_rep = state_to_representation_last(state=state, action_set=self.action_set, slot_set=self.slot_set,disease_symptom=self.disease_symptom, max_turn=self.parameter['max_turn'])
        next_state_rep = state_to_representation_last(state=next_state, action_set=self.action_set,slot_set=self.slot_set, disease_symptom=self.disease_symptom, max_turn=self.parameter['max_turn'])
        master_state_rep = state_to_representation_last(state=self.master_state, action_set=self.action_set,slot_set=self.slot_set, disease_symptom=self.disease_symptom, max_turn=self.parameter['max_turn'])
        # samples of master agent.
        self.master_reward += reward
        if self.sub_task_terminal is False:
            pass
        else:
            self.experience_replay_pool.append((master_state_rep, self.master_action_index, self.master_reward, next_state_rep, episode_over))

        # samples of lower agent. Only when this current session is not over.
        if episode_over is False:
            self.sub_task_terminal, _, self.intrinsic_reward = self.intrinsic_critic(next_state, self.master_action_index)

            goal = np.zeros(self.output_size)
            goal[self.master_action_index] = 1
            state_rep = np.concatenate((state_rep, goal), axis=0)
            next_state_rep = np.concatenate((next_state_rep, goal), axis=0)

            #如果达到固定长度，同时去掉即将删除transition的计数。
            self.visitation_count[self.master_action_index, agent_action] += 1
            if len(self.lower_agent.experience_replay_pool) == self.lower_agent.experience_replay_pool.maxlen:
                _, pre_agent_action, _, _, _, pre_master_action = self.lower_agent.experience_replay_pool.popleft()
                self.visitation_count[pre_master_action, pre_agent_action] -= 1
            self.lower_agent.experience_replay_pool.append((state_rep, agent_action, self.intrinsic_reward, next_state_rep, self.sub_task_terminal, self.master_action_index))
            # self.lower_agent.experience_replay_pool.append((state_rep, agent_action, reward, next_state_rep, self.sub_task_terminal, self.master_action_index))# extrinsic reward is returned to lower agent directly.

    def flush_pool(self):
        self.experience_replay_pool = deque(maxlen=self.parameter.get("experience_replay_pool_size"))
        self.lower_agent.flush_pool()
        self.visitation_count = np.zeros(shape=(self.output_size, len(self.lower_agent.action_space))) # [goal_num, lower_action_num]

    def intrinsic_critic(self, state, master_action_index):
        sub_task_terminate = False
        intrinsic_reward = 0
        inform_disease = False

        goal_list = [i for i in range(self.output_size)]
        state_batch = [state] * self.output_size
        similarity_score = self.internal_critic.get_similarity_state_dict(state_batch, goal_list)[master_action_index]

        if similarity_score > 0.98:
            sub_task_terminate = True
            inform_disease = True
            intrinsic_reward = similarity_score

        if similarity_score < 1e-3:
            sub_task_terminate = True
            inform_disease = False
            intrinsic_reward = 1 - similarity_score

        if self.sub_task_turn > 4:
            sub_task_terminate = True
            intrinsic_reward = -1

        return sub_task_terminate, inform_disease, intrinsic_reward

    def reward_shaping(self, lower_agent_action, goal):
        prob_action_goal = self.visitation_count[goal, lower_agent_action] / (self.visitation_count.sum() + 1e-8)
        prob_goal = self.visitation_count.sum(1)[goal] / (self.visitation_count.sum() + 1e-8)
        prob_action = self.visitation_count.sum(0)[lower_agent_action] / (self.visitation_count.sum() + 1e-8)
        return np.log(prob_action_goal / (prob_action * prob_goal + 1e-8))