from typing import Optional, Tuple

import torch
import torch.nn as nn
from transformers import AutoModel, AutoModelForCausalLM, AutoTokenizer


class ActorModel(nn.Module):
    """Actor model.

    Args:
        pretrained (str): Pretrained model name or path.
        config (GPT2Config): Model config.
        checkpoint (bool): Enable gradient checkpointing.
    """
    def __init__(self,
                 pretrained: Optional[str] = None,
                 debug: bool = False) -> None:
        super().__init__()

        self.tokenizer = AutoTokenizer.from_pretrained(pretrained,
                                                       padding_side='left')
        # galactica tokenizer eos_token is None
        if self.tokenizer.eos_token is None:
            self.tokenizer.eos_token = '</s>'
            self.tokenizer.eos_token_id = 0
        self.tokenizer.pad_token = self.tokenizer.eos_token
        self.tokenizer.pad_token_id = self.tokenizer.eos_token_id
        self.model = AutoModelForCausalLM.from_pretrained(pretrained)
        self.debug = debug

    def forward(self, inputs_ids: torch.Tensor,
                attention_mask: torch.Tensor) -> torch.Tensor:
        """Generate logits to have probability distribution over the vocabulary
        of the actions.

        Args:
            sequences (torch.Tensor): Sequences of states and actions used to
                    compute token logits for the whole list of sequences
            attention_mask (torch.Tensor): Mask for the sequences attention

        Returns:
            logits (torch.Tensor): Logits for the actions taken
        """
        model_output = self.model(inputs_ids, attention_mask=attention_mask)
        # need to return logits for the actions
        model_output = model_output.logits
        if self.debug:
            print('ActorModel.forward')
            print('model_output_logits shape', model_output.shape)
            print('model_output logits', model_output)
        return model_output

    @torch.no_grad()
    def generate(self, states: torch.Tensor,
                 state_mask: torch.Tensor) -> Tuple:
        """Generate actions and sequences=[states, actions] from state (i.e.
        input of the prompt generator model)

        Args:
            state (torch.Tensor): the input of the user
            state_mask (torch.Tensor): Mask for the state input (for padding)

        Returns:
            actions (torch.Tensor): Actions generated from the state
            sequences (torch.Tensor): Sequences generated from the
                state as [states, actions]
        """
        temperature = self.config.temperature
        # max sequence length for the actor (i.e. prompt + completion)
        # from config file - it depends by the model used
        max_sequence_length = self.config.max_sequence_length
        # max tokens generated by the actor (completion only) from config file
        max_tokens = self.config.max_tokens
        # temperature for the actor
        max_generation_possible = max_sequence_length - states.shape[1]
        # take the minimum between the maximum token that you want to generate
        # and the token that is possible to generate given the maximum sequence
        # supported
        max_completion = min(max_tokens, max_generation_possible)
        if max_completion <= 0:
            raise ValueError(
                'The maximum completion available is <= 0 the prompt is too ' +
                'long w.r.t the model sequence length')
        # the max_length is then the input length + the completion length
        max_length = states.shape[1] + max_completion
        # generate
        sequences = self.model.generate(
            input_ids=states,
            attention_mask=state_mask,
            temperature=temperature,
            max_length=max_length,
        )
        actions = sequences[:, states.shape[1]:]  # noqa E203
        if self.debug:
            print('ActorModel.generate')
            print('state', states)
            print('state shape', states.shape)
            print('sequence shape', sequences.shape)
            print('sequence', sequences)
            print('actions shape', actions.shape)
            print('actions', actions)
        return actions, sequences


class CriticModel(nn.Module):
    """GPT Critic model.

    Args:
        pretrained (str): Pretrained model name or path.
        config (GPT2Config): Model config.
        checkpoint (bool): Enable gradient checkpointing.
    """
    def __init__(self,
                 pretrained: Optional[str] = None,
                 debug: bool = True) -> None:

        self.tokenizer = AutoTokenizer.from_pretrained(
            pretrained,
            padding_side='left',
            truncation_side='left',
        )
        self.model = AutoModel.from_pretrained(pretrained)
        # galactica tokenizer eos_token is None
        if self.tokenizer.eos_token is None:
            self.tokenizer.eos_token = '</s>'
            self.tokenizer.eos_token_id = 0
        self.tokenizer.pad_token = self.tokenizer.eos_token
        self.tokenizer.pad_token_id = self.tokenizer.eos_token_id

        self.debug = debug
        # store config
        self.config = self.model.config
        # initialize the self.model
        head_hidden_size = self.config.model_head_hidden_size

        self.value_head = nn.Sequential(
            torch.nn.Linear(head_hidden_size, head_hidden_size),
            torch.nn.ReLU(),
            torch.nn.Linear(head_hidden_size, 1),
            Rearrange('... 1 -> ...'),
        )

    def forward(self, input_ids: torch.Tensor,
                attention_mask: torch.Tensor) -> torch.Tensor:
        """Generate the sequence of rewards for the given output sequence what
        is the quality of the output sequence tokens?

        Args:
            output_sequence (torch.Tensor): The sequence of tokens to be
                evaluated
            output_sequence_mask (torch.Tensor): Mask for the attention

        Returns:
            torch.Tensor: Rewards for the given output sequence
        """
        output = self.model(input_ids,
                            attention_mask=attention_mask,
                            return_dict=True)
        # What if the output_sequence is longer than the max context of
        # the model?
        rewards = self.value_head(output.last_hidden_state)
        if self.debug:
            print('RewardModel.forward')
            print('output_sequence.shape', input_ids.shape)
            print('output_sequence', input_ids)
            print('reward.shape', rewards.shape)
            print('reward', rewards)
        return rewards

    def get_reward(self, input_ids: torch.Tensor,
                   attention_mask: torch.Tensor) -> torch.Tensor:
        """Get the reward for the given output sequence.

        Args:
            output_sequence (torch.Tensor): The concatenation of initial input
                and actor output as tokens
            output_sequence_mask (torch.Tensor): Mask for the attention
        """
        rewards = self.forward(input_ids, attention_mask)
        return rewards[:, -1]


class ActorCritic(nn.Module):
    """Actor Critic class stores both the actor and the critic models and it
    generates values and action for given sequences during the training of the
    actor.

    Attributes:
        actor (ActorModel): Actor model
        critic (CriticModel): Critic model
        debug (bool): enable prints for Debugging

    Methods:
        forward: given a sequence returns action logits and values (used
            to evaluate the actor during training)
        generate: given a sequence returns action, action logits, values
            sequences and sequences masks (used to generate new sequences
            during acting phase)
    """
    def __init__(
        self,
        actor: nn.Module = None,
        critic: nn.Module = None,
        debug: bool = False,
    ) -> None:
        super().__init__()
        self.actor = actor,
        self.critic = critic
        self.debug = debug

    def forward(
        self,
        sequences: torch.Tensor,
        sequences_mask: torch.Tensor,
        action_len: int,
    ) -> Tuple:
        """Given the whole sequences, use the actor forward to get the logits
        for each token in the sequence and the critic forward to get the values
        for each generation step.

        Args:
            sequences (torch.Tensor): Sequences composed of [states, actions]
            sequence_mask (torch.Tensor): Mask for the sequences
            action_length (int): Length of the actions in the sequences

        Returns:
            action_logits (torch.Tensor): Logits for the actions in the
                sequences
            values (torch.Tensor): Values for the actions in the sequences
        """
        # use a single forward on the whole sequence
        # to get pi(y | x) and ignore predicted output
        actions_logits = self.actor(sequences, sequences_mask)
        values = self.critic.forward(sequences, sequences_mask)

        # return only logits and values for the actions taken
        real_actions_logits = actions_logits[:, -action_len:, :]
        real_values = values[:, -action_len:]

        if self.debug:
            print('ActorCritic.forward')
            print('action_len', action_len)
            print('sequences.shape', sequences.shape)
            print('sequences', sequences)
            print('real_action_logits.shape', actions_logits.shape)
            print('real_action_logits', actions_logits)
            print('real_values.shape', values.shape)
            print('real_values', values)

        return (
            real_actions_logits,
            real_values,
        )

    @torch.no_grad()
    def generate(self, states: torch.Tensor,
                 state_mask: torch.Tensor) -> Tuple:
        """Generate actions, actions_logits, values and sequences from states.

        Args:
            states (torch.Tensor): user inputs
            state_mask (torch.Tensor): Mask for the states of the environment

        Returns:
            actions (torch.Tensor): Actions generated from the states
            actions_logits (torch.Tensor): Logits for the actions generated
                from the states (i.e. pi(y | x))
            values (torch.Tensor): Values generated by the critic model
                for the actions generated by the actor (i.e. V(x))
            sequences (torch.Tensor): Sequences generated from the states
                as [states, actions]
        """
        # generate action sequence
        actions, sequence = self.actor.generate(states, state_mask)
        sequences_mask = sequence != self.actor.tokenizer.pad_token_id
        sequences_mask = sequences_mask.to(sequence.device).long().detach()
        action_len = actions.shape[1]

        # generate actions_logits and values
        actions_logits, values = self.forward(sequence, sequences_mask,
                                              action_len)
        if self.debug:
            print('ActorCritic.generate')
            print('actions shape', actions.shape)
            print('actions', actions)
            print('sequence shape', sequence.shape)
            print('sequence', sequence)
            print('actions_logits shape', actions_logits.shape)
            print('actions_logits', actions_logits)
            print('values shape', values.shape)
            print('values', values)

        return actions, actions_logits, values, sequence, sequences_mask
