import logging
from typing import Set

from envision.types import format_actor_id

from .mission_planner import MissionPlanner
from .scenario import Scenario
from .sensors import Sensors
from .vehicle import VehicleState, Vehicle
from .controllers import ActionSpaceType
from .remote_agent_buffer import RemoteAgentBuffer


class AgentManager:
    """Tracks agent states and implements methods for managing agent life cycle.

    XXX: It is very likely that this class will see heavy changes in the near future.
         Documentation for specific attributes and methods will be supplied at a later
         time.
    """

    def __init__(self, interfaces):
        self._log = logging.getLogger(self.__class__.__name__)
        self._remote_agent_buffer = RemoteAgentBuffer()

        self._ego_agent_ids = set()
        self._social_agent_ids = set()
        self._vehicle_with_sensors = dict()

        # Initial interfaces are for agents that are spawned at the beginning of the
        # episode and that we'd re-spawn upon episode reset. This would include ego
        # agents and social agents defined in SStudio. Hijacking agents in bubbles
        # would not be included
        self._initial_interfaces = interfaces
        self._pending_agent_ids = set()

        # Agent interfaces are interfaces for _all_ active agents
        self._agent_interfaces = {}

        # Agent data models
        self._agent_data_models = {}

        # We send observations and receive actions for all values in this dictionary
        self._remote_social_agents = {}

    def teardown(self):
        self._log.debug("Tearing down AgentManager")
        self.teardown_ego_agents()
        self.teardown_social_agents()
        self._vehicle_with_sensors = dict()

    def destroy(self):
        self._remote_agent_buffer.destroy()

    @property
    def agent_ids(self):
        return self._ego_agent_ids | self._social_agent_ids

    @property
    def ego_agent_ids(self):
        return self._ego_agent_ids

    @property
    def social_agent_ids(self):
        return self._social_agent_ids

    @property
    def agent_interfaces(self):
        return self._agent_interfaces

    def agent_interface_for_agent_id(self, agent_id):
        return self._agent_interfaces.get(agent_id, None)

    @property
    def pending_agent_ids(self):
        """The IDs of agents that are waiting to enter the simulation"""
        return self._pending_agent_ids

    @property
    def active_agents(self):
        return self.agent_ids - self.pending_agent_ids

    def is_ego(self, agent_id):
        return agent_id in self.ego_agent_ids

    def remove_pending_agent_ids(self, agent_ids):
        assert agent_ids.issubset(self.agent_ids)
        self._pending_agent_ids -= agent_ids

    def observe_from(self, sim, vehicle_ids):
        observations = {}
        rewards = {}
        dones = {}
        scores = {}
        for v_id in vehicle_ids:
            vehicle = sim.vehicle_index.vehicle_by_id(v_id)
            agent_id = self._vehicle_with_sensors[v_id]
            sensor_state = sim.vehicle_index.sensor_state_for_vehicle_id(vehicle.id)
            observations[agent_id], dones[agent_id] = Sensors.observe(
                sim, agent_id, sensor_state, vehicle
            )
            rewards[agent_id] = vehicle.trip_meter_sensor(increment=True)
            scores[agent_id] = vehicle.trip_meter_sensor()

        return observations, rewards, scores, dones

    def observe(self, sim):
        observations = {}
        rewards = {}
        scores = {}
        dones = {
            agent_id: agent_id not in self.pending_agent_ids
            for agent_id in self.agent_ids
            if agent_id not in sim.vehicle_index.agent_vehicle_ids
        }

        for agent_id in self.active_agents:
            agent_interface = self._agent_interfaces[agent_id]
            # An agent may be pointing to its own vehicle or observing a social vehicle

            indices = sim.vehicle_index.vehicle_indices_by_actor_id(
                agent_id, columns=["vehicle_id", "shadow_actor_id"],
            )

            if agent_interface.action_space == ActionSpaceType.MultiTargetPose:
                vehicles = [
                    sim.vehicle_index.vehicle_by_id(vehicle_id)
                    for vehicle_id in indices["vehicle_id"]
                ]
                # returns format of {<agent_id>: {<vehicle_id>: {...}}}
                sensor_states = {
                    vehicle.id: sim.vehicle_index.sensor_state_for_vehicle_id(
                        vehicle.id
                    )
                    for vehicle in vehicles
                }
                observations[agent_id], dones[agent_id] = Sensors.observe_batch(
                    sim, agent_id, sensor_states, {v.id: v for v in vehicles}
                )

                # XXX: For now we collapse all vehicle dones into a single done
                dones[agent_id] = any(dones[agent_id].values())

                rewards[agent_id] = {
                    vehicle_id: self._vehicle_reward(vehicle_id, sim)
                    for vehicle_id, sensor_state in sensor_states.items()
                }
                scores[agent_id] = {
                    format_actor_id(
                        agent_id, vehicle_id, is_multi=True
                    ): self._vehicle_score(vehicle_id, sim)
                    for (vehicle_id, sensor_state) in sensor_states.items()
                }
            else:
                assert len(indices["vehicle_id"]) == 1, (
                    "Unless the action space is MultiTargetPose we "
                    f"should only have a single vehicle under agent_id={agent_id}\n"
                    f"vehicle_ids={indices['vehicle_id']}"
                )

                vehicle = sim.vehicle_index.vehicle_by_id(indices["vehicle_id"][0])
                sensor_state = sim.vehicle_index.sensor_state_for_vehicle_id(vehicle.id)
                observations[agent_id], dones[agent_id] = Sensors.observe(
                    sim, agent_id, sensor_state, vehicle
                )

                # It is not a shadow agent's fault if it is done
                if indices["shadow_actor_id"][0] == agent_id:
                    dones[agent_id] = False
                else:
                    logging.log(
                        logging.DEBUG,
                        f"Agent `{agent_id}` has raised done with {observations[agent_id].events}",
                    )

                rewards[agent_id] = vehicle.trip_meter_sensor(increment=True)
                scores[agent_id] = vehicle.trip_meter_sensor()

        return observations, rewards, scores, dones

    def _vehicle_reward(self, vehicle_id, sim):
        return sim.vehicle_index.vehicle_by_id(vehicle_id).trip_meter_sensor(
            increment=True
        )

    def _vehicle_score(self, vehicle_id, sim):
        return sim.vehicle_index.vehicle_by_id(vehicle_id).trip_meter_sensor()

    def step_agent_sensors(self, sim):
        for vehicle_id in sim.vehicle_index.agent_vehicle_ids:
            sensor_state = sim.vehicle_index.sensor_state_for_vehicle_id(vehicle_id)
            Sensors.step(self, sensor_state)

            vehicle = sim.vehicle_index.vehicle_by_id(vehicle_id)
            for sensor in vehicle.sensors.values():
                sensor.step()

    def _filter_for_active_ego(self, dict_):
        return {
            id_: dict_[id_]
            for id_ in self._ego_agent_ids
            if not id_ in self.pending_agent_ids
        }

    def filter_response_for_ego(self, response_tuple):
        return tuple(map(self._filter_for_active_ego, response_tuple))

    def fetch_agent_actions(self, sim, ego_agent_actions):
        social_agent_actions = {
            agent_id: remote_agent.recv_action(timeout=5)
            for agent_id, remote_agent in self._remote_social_agents.items()
        }

        agents_without_actions = [
            agent_id
            for (agent_id, action) in social_agent_actions.items()
            if action is None
        ]
        if len(agents_without_actions) > 0:
            self._log.debug(
                f"social_agents=({', '.join(agents_without_actions)}) returned no action"
            )

        social_agent_actions = self._filter_social_agent_actions_for_controlled_vehicles(
            sim, social_agent_actions
        )

        return {**ego_agent_actions, **social_agent_actions}

    def _filter_social_agent_actions_for_controlled_vehicles(
        self, sim, social_agent_actions
    ):
        """Some agents may not be controlling a vehicle, such as when a vehicle is in
        the airlock, where the agent is observing and running its policy, but the
        returned action should not be executed on the vehicle until it is hijacked
        by the agent.
        """
        vehicle_ids_controlled_by_agents = sim.vehicle_index.agent_vehicle_ids
        controlling_agent_ids = set(
            [
                sim.vehicle_index.actor_id_from_vehicle_id(v_id)
                for v_id in vehicle_ids_controlled_by_agents
            ]
        )

        social_agent_actions = {
            agent_id: social_agent_actions[agent_id]
            for agent_id in social_agent_actions
            if agent_id in controlling_agent_ids
        }

        # Handle boids where some vehicles are hijacked and some have not yet been
        for agent_id, actions in social_agent_actions.items():
            agent_interface = self._agent_interfaces[agent_id]
            if agent_interface.action_space == ActionSpaceType.MultiTargetPose:
                controlled_vehicle_ids = sim.vehicle_index.vehicle_ids_by_actor_id(
                    agent_id, include_shadowers=False
                )
                social_agent_actions[agent_id] = {
                    vehicle_id: vehicle_action
                    for vehicle_id, vehicle_action in actions.items()
                    if vehicle_id in controlled_vehicle_ids
                }

        return social_agent_actions

    def send_observations_to_social_agents(self, observations):
        # TODO: Don't send observations (or receive actions) from agents that have done
        #       vehicles.
        for agent_id, remote_agent in self._remote_social_agents.items():
            obs = observations[agent_id]
            remote_agent.send_observation(obs)

    def setup_agents(self, sim):
        self.init_ego_agents(sim)
        self.setup_social_agents(sim)

    def init_ego_agents(self, sim):
        for agent_id, agent_interface in self._initial_interfaces.items():
            # TODO: Remove `pending_agent_ids`
            self.pending_agent_ids.add(agent_id)
            self._ego_agent_ids.add(agent_id)
            self.agent_interfaces[agent_id] = agent_interface

    def setup_social_agents(self, sim):
        social_agents = sim.scenario.social_agents
        self._remote_social_agents = {
            agent_id: self._remote_agent_buffer.acquire_remote_agent()
            for agent_id in social_agents
        }

        for agent_id, (social_agent, social_agent_model) in social_agents.items():
            self._add_agent(
                agent_id,
                social_agent.interface,
                social_agent_model,
                sim,
                trainable=False,
            )
            self._social_agent_ids.add(agent_id)

        for social_agent_id, remote_social_agent in self._remote_social_agents.items():
            remote_social_agent.start(social_agents[social_agent_id][0])

    def _add_agent(self, agent_id, agent_interface, agent_model, sim, trainable=True):
        # TODO: Disentangle what is entangled below into:
        # 1. AgentState initialization,
        # 2. Agent vehicle initialization, which should live elsewhere, and
        # 3. Provider related state initialization, which does not belong here.
        #
        # A couple of things forced the current unhappy state --
        #
        # 1. SMARTS internal coordinate system should be 100% unified. Coordinate
        #    transformation should happen only at the interface between SMARTS and
        #    its providers. For example, mission start pose should just be vehicle
        #    initial pose.
        # 2. AgentState should be allowed to fully initialized (setup) without vehicle
        #    information. We currently rely on the creation of the Vehicle instance to
        #    do the coordinate transformation. :-(
        # 3. Providers should not be creating vehicles. They do need to get notified
        #    about new vehicles entering their territory through the VehicleState
        #    message. But that does not need to happen at Agent instantiation.

        assert isinstance(agent_id, str)  # SUMO expects strings identifiers

        scenario = sim.scenario
        mission = scenario.mission(agent_id)
        planner = MissionPlanner(scenario.waypoints, scenario.road_network)
        planner.plan(mission)

        vehicle = sim.vehicle_index.build_agent_vehicle(
            sim,
            agent_id,
            agent_interface,
            planner,
            scenario.vehicle_filepath,
            scenario.tire_parameters_filepath,
            trainable,
            scenario.surface_patches,
            scenario.controller_parameters_filepath,
            agent_model.initial_speed,
        )

        for provider in sim.providers:
            if agent_interface.action_space in provider.action_spaces:
                provider.create_vehicle(
                    VehicleState(
                        vehicle_id=vehicle.id,
                        vehicle_type=vehicle.vehicle_type,
                        pose=vehicle.pose,
                        dimensions=vehicle.chassis.dimensions,
                        source="NEW-AGENT",
                    )
                )

        self._agent_interfaces[agent_id] = agent_interface
        self._agent_data_models[agent_id] = agent_model

    def start_social_agent(self, agent_id, social_agent, agent_model):
        remote_agent = self._remote_agent_buffer.acquire_remote_agent()
        remote_agent.start(social_agent)
        self._remote_social_agents[agent_id] = remote_agent
        self._agent_interfaces[agent_id] = social_agent.interface
        self._social_agent_ids.add(agent_id)
        self._agent_data_models[agent_id] = agent_model

    def teardown_ego_agents(self, filter_ids: Set = None):
        ids_ = self._teardown_agents_by_ids(self._ego_agent_ids, filter_ids)
        self._ego_agent_ids -= ids_
        return ids_

    def teardown_social_agents(self, filter_ids: Set = None):
        ids_ = self._teardown_agents_by_ids(self._social_agent_ids, filter_ids)

        for id_ in ids_:
            self._remote_social_agents[id_].terminate()
            del self._remote_social_agents[id_]
            del self._agent_data_models[id_]

        self._social_agent_ids -= ids_
        return ids_

    def _teardown_agents_by_ids(self, agent_ids, filter_ids: Set):
        ids_ = agent_ids.copy()
        if filter_ids is not None:
            ids_ = ids_ & filter_ids

        if ids_:
            self._log.debug(f"Tearing down agents={ids_}")

        for agent_id in ids_:
            self._agent_interfaces.pop(agent_id, None)

        return ids_

    def reset_agents(self, observations):
        for agent_id, remote_agent in self._remote_social_agents.items():
            obs = observations[agent_id]
            remote_agent.send_observation(obs)

        # Observations contain those for social agents; filter them out
        return self._filter_for_active_ego(observations)

    def name_for_agent(self, agent_id):
        if agent_id not in self._agent_data_models:
            return ""

        return self._agent_data_models[agent_id].name

    def attach_sensors_to_vehicles(self, sim, agent_interface, vehicle_ids):
        for sv_id in vehicle_ids:

            if sv_id in self._vehicle_with_sensors:
                continue

            mission_planner = MissionPlanner(
                sim.scenario.waypoints, sim.scenario.road_network
            )

            mission_planner.plan(mission=None)

            agent_id = f"Agent-{sv_id}"
            self._vehicle_with_sensors[sv_id] = agent_id
            self._agent_interfaces[agent_id] = agent_interface

            sim.vehicle_index.attach_sensors_to_vehicle(
                sim, sv_id, agent_interface, mission_planner
            )
