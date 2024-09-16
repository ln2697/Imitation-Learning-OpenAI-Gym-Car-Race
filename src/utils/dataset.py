from __future__ import annotations

import os

import numpy as np
import torch
from dynaconf import Dynaconf
from pyparsing import deque
from torch.utils.data import Dataset
from tqdm import tqdm

from src.utils.balance import discretize_and_compute_balancing_weights

conf = Dynaconf(settings_files=["conf/default_conf.py"])


def convert_action_gym_to_models(action):
    """
    Convert the action from the gym environment format to the models format.
    Args:
        action (numpy.ndarray): The action array in the gym environment format.
    Returns:
        numpy.ndarray: The converted action array in the models format.
    """
    if isinstance(action, torch.Tensor):
        action = action.detach().cpu().numpy()
    assert not np.any(
        (action[:, 1] > 0.0) & (action[:, 2] > 0.0)
    ), "No projection possible. Both throttle and brake found in at least one action."
    converted_actions = np.zeros((action.shape[0], 2))
    converted_actions[:, 0] = action[:, 0]  # Steering remains unchanged
    converted_actions[:, 1] = action[:, 1] - action[:, 2]  # acceleration = throttle - brake
    return converted_actions


def convert_action_models_to_gym(outputs):
    """
    Converts the action models to the gym format.
    Parameters:
    - outputs: numpy.ndarray
        The output array containing the action models.
    Returns:
    - numpy.ndarray
        The converted controls array in the gym format.
    """
    if isinstance(outputs, torch.Tensor):
        outputs = outputs.detach().cpu().numpy()
    controls = np.zeros((len(outputs), 3))
    controls[:, 0] = outputs[:, 0]
    controls[:, 1][outputs[:, 1] >= 0] = outputs[:, 1][outputs[:, 1] >= 0]
    controls[:, 2][outputs[:, 1] < 0] = -outputs[:, 1][outputs[:, 1] < 0]
    return controls


def extract_observation_features(image):
    """
    Extracts observation features from an image.
    Parameters:
    - image: numpy.ndarray
        The input image.
    Returns:
    - numpy.ndarray
        The extracted observation features.
    """
    image = image.copy()
    if len(image.shape) == 3:
        image = image[None, :, :, :]
    x_min, x_max = 45, 52
    y_min, y_max = 65, 80
    image = image[:, :84, :, :]

    image[:, y_min:y_max, x_min:x_max] = [105, 105, 105]
    chevrons = image[:, :, :, 0] > 150
    road = np.abs(image.mean(axis=3) - 105) < 15
    # TODO: make both masks disjoint
    masks = np.stack([chevrons, road], axis=3)
    return np.transpose(masks, (0, 3, 1, 2))


def preprocess_obs(obs):
    """
    Preprocesses the observation.
    """
    obs = obs.copy()
    if len(obs.shape) == 3:
        obs = obs[None, :, :, :]
    obs = obs[:, :84, :, :]
    if not conf.USE_RGB:
        obs = obs.mean(axis=-1, keepdims=True)
    obs = np.transpose(obs, (0, 3, 1, 2))
    obs = (obs - conf.OBS_MEAN) / conf.OBS_STD
    return obs


def preprocess_input_testing(obs, speed, wheels_omegas, angular_velocity, steering_joint_angle, do_process_obs=True):
    """
    Preprocesses the input data for a race car model.
    Args:
        obs (ndarray): The observation data of the race car.
        speed (ndarray): The speed data of the race car.
        wheels_omegas (ndarray): The wheel omegas data of the race car.
        angular_velocity (ndarray): The angular velocity data of the race car.
        steering_joint_angle (ndarray): The steering joint angle data of the race car.
    Returns:
        tuple: A tuple containing the preprocessed observation data and state data.
    """
    if do_process_obs:
        obs = preprocess_obs(obs)

    speed = np.array((speed - conf.SPEED_MEAN) / conf.SPEED_STD).reshape(-1, 1)

    wheels_omegas = np.array(wheels_omegas).reshape(-1, 4)

    wheels_omegas_std = np.std(wheels_omegas, axis=1).reshape(-1, 1)
    wheels_omegas_std = (wheels_omegas_std - conf.WHEEL_OMEGA_STD_MEAN) / conf.WHEEL_OMEGA_STD_STD

    wheels_omegas = ((wheels_omegas - conf.WHEEL_OMEGA_MEAN) / conf.WHEEL_OMEGA_STD).reshape(-1, 4)

    angular_velocity = np.array((angular_velocity - conf.ANGULAR_VELOCITY_MEAN) / conf.ANGULAR_VELOCITY_STD).reshape(
        -1, 1
    )

    steering_joint_angle = np.array(
        (steering_joint_angle - conf.STEERING_JOINT_ANGLE_MEAN) / conf.STEERING_JOINT_ANGLE_STD
    ).reshape(-1, 1)

    state = np.concatenate(
        (
            speed,
            wheels_omegas,
            wheels_omegas_std,
            angular_velocity,
            steering_joint_angle,
        ),
        axis=-1,
    )

    return obs, state


def preprocess_input_training(obs, speed, wheels_omegas, angular_velocity, steering_joint_angle, action, curvature):
    """
    Preprocesses the input data for a race car model.
    Args:
        obs (ndarray): The observation data of the race car.
        speed (ndarray): The speed data of the race car.
        wheels_omegas (ndarray): The wheel omegas data of the race car.
        angular_velocity (ndarray): The angular velocity data of the race car.
        steering_joint_angle (ndarray): The steering joint angle data of the race car.
        action (ndarray): The expert action
    Returns:
        tuple: A tuple containing the preprocessed observation data and state data.
    """
    curvature = np.array((curvature - conf.CURVATURE_MEAN) / conf.CURVATURE_STD).reshape(-1, 1)

    masks = extract_observation_features(obs)

    obs, state = preprocess_input_testing(
        obs, speed, wheels_omegas, angular_velocity, steering_joint_angle, do_process_obs=False
    )

    action = convert_action_gym_to_models(action).astype(np.float32)

    return (
        obs.astype(np.uint8),
        state.astype(np.float32),
        curvature.astype(np.float32),
        masks.astype(np.uint8),
        action.astype(np.float32),
    )


class AbstractDataset(Dataset):
    def __init__(self) -> None:
        super().__init__()
        # Initialize tensors to save memory
        self.observation = deque(maxlen=conf.IMITATION_MAX_MEMORY)
        self.state = deque(maxlen=conf.IMITATION_MAX_MEMORY)
        self.action = deque(maxlen=conf.IMITATION_MAX_MEMORY)
        self.curvature = deque(maxlen=conf.IMITATION_MAX_MEMORY)
        self.masks = deque(maxlen=conf.IMITATION_MAX_MEMORY)

    def append(self, obs, speed, wheels_omegas, angular_velocity, steering_joint_angle, action, curvature):
        # Preprocessing
        obs, state, curvature, masks, action = preprocess_input_training(
            obs=obs,
            speed=speed,
            wheels_omegas=wheels_omegas,
            angular_velocity=angular_velocity,
            steering_joint_angle=steering_joint_angle,
            action=action,
            curvature=curvature,
        )

        # Store preprocessed data into tensor
        if conf.USE_RGB:
            self.observation += list(obs)
        else:
            self.observation += list(obs.mean(axis=-1, keepdims=True))
        self.state += list(state)
        self.curvature += list(curvature)
        self.masks += list(masks)
        self.action += list(action)

    def recompute_weights(self):
        action = np.array(self.action)
        self.instance_weights = torch.from_numpy(
            discretize_and_compute_balancing_weights(action, bins=conf.IMITATION_BALANCE_BINS).reshape(-1, 1)
        ).float()
        return self.instance_weights

    def save(self):
        pass

    def __len__(self):
        return len(self.observation)

    def __getitem__(self, idx):
        obs = preprocess_obs(self.observation[idx].astype(np.float64))
        if len(obs.shape) == 4:
            obs = obs.squeeze(axis=0)
        return (
            torch.tensor(obs).double(),
            torch.tensor(self.state[idx]).double(),
            torch.tensor(self.action[idx]).double(),
            torch.tensor(self.instance_weights[idx]).double(),
            torch.tensor(self.curvature[idx]).double(),
            torch.tensor(self.masks[idx]).double(),
        )


class DaggerDataset(AbstractDataset):
    def __init__(self, record_files):
        super().__init__()
        record_files = [f for f in record_files if ".npz" in f]
        for record_path in tqdm(record_files, desc=f"Read {len(record_files)} data files"):
            if os.path.islink(record_path):
                record_path = os.readlink(record_path)
            record = np.load(record_path)
            self.append(
                obs=record["observation_history"][:: conf.IMITATION_DATASET_SAMPLING_RATE],
                speed=record["speed_history"][:: conf.IMITATION_DATASET_SAMPLING_RATE],
                wheels_omegas=record["wheels_omegas_history"][:: conf.IMITATION_DATASET_SAMPLING_RATE],
                angular_velocity=record["angular_velocity_history"][:: conf.IMITATION_DATASET_SAMPLING_RATE],
                steering_joint_angle=record["steering_joint_angle_history"][:: conf.IMITATION_DATASET_SAMPLING_RATE],
                action=record["action_history"][:: conf.IMITATION_DATASET_SAMPLING_RATE],
                curvature=record["curvature_history"][:: conf.IMITATION_DATASET_SAMPLING_RATE],
            )
        self.instance_weights = self.recompute_weights()


class ImitationDataset(DaggerDataset):
    def __init__(self, dataset_path):
        # Initialize tensors to save memory
        files: list[str] = sorted(os.listdir(dataset_path))
        record_files = [f for f in files if ".npz" in f]
        record_files = [os.path.join(dataset_path, f) for f in record_files]
        super().__init__(record_files)
