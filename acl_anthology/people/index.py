# Copyright 2023 Marcel Bollmann <marcel@bollmann.me>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from collections import defaultdict
import itertools as it
from pathlib import Path
from rich.progress import track
from scipy.cluster.hierarchy import DisjointSet  # type: ignore
from typing import TYPE_CHECKING
import yaml

try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader  # type: ignore

from ..exceptions import AmbiguousNameError, NameIDUndefinedError
from ..utils.logging import get_logger
from . import Person, Name, NameSpecification

if TYPE_CHECKING:
    from ..anthology import Anthology
    from ..collections import Paper, Volume

log = get_logger()
VARIANTS_FILE = "yaml/name_variants.yaml"


class PersonIndex:
    """Index object through which all persons (authors/editors) can be accessed.

    Attributes:
        parent: The parent Anthology instance to which this index belongs.
    """

    def __init__(self, parent: Anthology) -> None:
        self.parent: Anthology = parent

        self.people: dict[str, Person] = {}
        """A mapping of IDs to [Person][acl_anthology.people.person.Person] instances."""

        self.name_to_ids: dict[Name, list[str]] = defaultdict(list)
        """A mapping of [Name][acl_anthology.people.name.Name] instances to person IDs."""

        self.similar: DisjointSet = DisjointSet()
        """A [disjoint-set structure][scipy.cluster.hierarchy.DisjointSet] of persons with similar names."""

        self.is_built = False
        """A flag indicating whether the index has been constructed."""

    def reset(self) -> None:
        """Resets the index."""
        self.people = {}
        self.name_to_ids = defaultdict(list)
        self.similar = DisjointSet()
        self.is_built = False

    def build(self, show_progress: bool = False) -> None:
        """Load the entire Anthology data and build an index of persons.

        Important:
            Exceptions raised during the index creation are sent to the logger, and **not** re-raised.
            Use the [SeverityTracker][acl_anthology.utils.logging.SeverityTracker] to check if an exception occurred.
        """
        self.reset()
        # Load variant list, so IDs defined there are added first
        self._load_variant_list()
        # Go through every single volume/paper and add authors/editors
        iterator = track(
            self.parent.collections,
            total=len(self.parent.collections),
            disable=(not show_progress),
            description="Building person index...",
        )
        for collection in iterator:
            for volume in collection:
                context: Paper | Volume = volume
                try:
                    for name_spec in volume.editors:
                        person = self.get_or_create_person(name_spec)
                        person.item_ids.add(volume.full_id_tuple)
                    for paper in volume:
                        context = paper
                        for name_spec in it.chain(paper.authors, paper.editors):
                            person = self.get_or_create_person(name_spec)
                            person.item_ids.add(paper.full_id_tuple)
                except Exception as exc:
                    exc.add_note(
                        (
                            f"Raised in {context.__class__.__name__} "
                            f"{context.full_id}; {name_spec}"
                        )
                    )
                    log.exception(exc)
        self.is_built = True

    def add_person(self, person: Person) -> None:
        """Add a new person to the index.

        Parameters:
            person: The person to add, which should not exist in the index yet.
        """
        if person.id in self.people:
            raise KeyError(f"A Person with ID '{person.id}' already exists in the index")
        self.people[person.id] = person
        for name in person.names:
            self.name_to_ids[name].append(person.id)

    def get_or_create_person(self, name_spec: NameSpecification) -> Person:
        """Get the person represented by a name specification, or create a new one if needed.

        Parameters:
            name_spec: The name specification on the paper, volume, etc.

        Returns:
            The person represented by `name_spec`.  This will try to use the `id` attribute if it is set, look up the name in the index otherwise, or try to find a matching person by way of an ID clash.  If all of these fail, it will create a new person and return that.

        Raises:
            AmbiguousNameError: If there are multiple known IDs for the given name, but there is no explicit `id` attribute.
            NameIDUndefinedError: If there is an explicit `id` attribute, but the ID has not been defined.
        """
        name = name_spec.name
        if (pid := name_spec.id) is not None:
            try:
                person = self.people[pid]
                person.add_name(name)
            except KeyError:
                raise NameIDUndefinedError(name_spec)
        elif pid_list := self.name_to_ids[name]:
            if len(pid_list) > 1:
                exc = AmbiguousNameError(
                    name,
                    f"Name '{name.as_first_last()}' is ambiguous, but was used without an ID",
                )
                exc.add_note(f"Known IDs are: {', '.join(pid_list)}")
                raise exc
            pid = pid_list[0]
            person = self.people[pid]
        else:
            pid = self.generate_id(name)
            try:
                # If the auto-generated ID already exists, we assume it's the same person
                person = self.people[pid]
                # If the name scores higher than the current canonical one, we
                # also assume we should set this as the canonical one
                if name.score() > person.canonical_name.score():
                    person.set_canonical_name(name)
                else:
                    person.add_name(name)
                self.name_to_ids[name].append(pid)
            except KeyError:
                # If it doesn't, only then do we create a new perosn
                person = Person(id=pid, names=[name])
                self.add_person(person)
        return person

    @staticmethod
    def generate_id(name: Name) -> str:
        """Generates and returns an ID from the given name.

        Warning:
            This **intentionally doesn't guarantee uniqueness** of the generated ID.
            If two names generate identical IDs with this method, we assume they
            refer to the same person.  This happens e.g. when there are missing
            accents in one version, or when we have an inconsistent first/last split
            for multiword names.  These cases have in practice always referred to
            the same person.
        """
        return name.slugify()

    def _load_variant_list(self) -> None:
        """Loads and parses the `name_variant.yaml` file.

        Raises:
            AmbiguousNameError: If there are ambiguous "canonical" names without explicit, unique IDs for each one.
        """
        filename = self.parent.datadir / Path(VARIANTS_FILE)
        with open(filename, "r") as f:
            variant_list = yaml.load(f, Loader=Loader)
        for entry in variant_list:
            # Every entry must have a "canonical" name
            canonical = Name.from_dict(entry["canonical"])
            # If it doesn't define an ID, we have to create one
            if (pid := entry.get("id")) is None:
                pid = self.generate_id(canonical)
                if pid in self.people:
                    raise AmbiguousNameError(
                        canonical,
                        (
                            f"While parsing {filename}: "
                            f"name '{canonical.as_first_last()}' is ambiguous, but the "
                            f"automatically generated ID '{pid}' already exists."
                        ),
                    )
            # Parse all the variant names, and make sure canonical stays at index 0
            names = [canonical] + [
                Name.from_dict(var) for var in entry.get("variants", [])
            ]
            # Now we can create a new person from this entry...
            person = Person(id=pid, names=names, comment=entry.get("comment", None))
            # ...and add it to the index
            self.add_person(person)
            # TODO: maybe refactor this later:
            if "similar" in entry:
                self.similar.add(pid)
                for similar_id in entry["similar"]:
                    self.similar.add(similar_id)
                    self.similar.merge(pid, similar_id)
