r"""
:mod:`mdsapt.sapt` -- Tools for calculating SAPT energy from MD data
====================================================================

Sets up and runs `SAPT <https://psicode.org/psi4manual/master/sapt.html>`_
calculations between the residues selected in the input file.

 autoclass:: SAPT
    :members:
    :inherited-members:

.. autoclass:: TrajectorySAPT
    :members:
    :inherited-members:

"""

from typing import Dict, List, Set, Tuple

import pandas as pd

import MDAnalysis as mda

from MDAnalysis.analysis.base import AnalysisBase
from MDAnalysis.topology.guessers import guess_types
from MDAnalysis.converters.RDKit import atomgroup_to_mol
from MDAnalysis.lib.log import ProgressBar

import psi4

from pydantic import ValidationError

from rdkit import Chem

from .config import Config, TrajectoryAnalysisConfig, DockingAnalysisConfig, DockingStructureMode
from .optimizer import Optimizer, get_spin_multiplicity
from .utils.ensemble import Ensemble, EnsembleAtomGroup

import logging

logger = logging.getLogger('mdsapt.sapt')


class SAPT(object):
    """Contains methods for running SAPT calculations on molecular dynamics data. Used as the super class for other
    SAPT tools in the library. """

    _opt: Optimizer
    _cfg: Config
    _mem: str
    _save_psi_out: bool
    _method: str
    _basis: str
    _settings: Dict[str, str]
    _mht_to_kcalmol: float = 627.509

    def __init__(self, config: Config, optimizer: Optimizer) -> None:
        self._opt = optimizer
        self._cfg = config
        self._mem = config.system_limits.memory
        self._save_psi_out = config.psi4.save_output
        self._method = config.psi4.method
        self._basis = config.psi4.basis
        self._settings = config.psi4.settings

    def get_psi_mol(self, key, resid: mda.AtomGroup) -> str:
        """Generates Psi4 input file the specified residue. Prepares amino acids for SAPT using
        :class:`mdsapt.optimizer.Optimizer`. Adds charge and spin multiplicity to top of cooridnates. """
        resid: mda.AtomGroup = self._opt.rebuild_resid(key, resid)
        rd_mol = atomgroup_to_mol(resid)

        coords: str = f'{Chem.GetFormalCharge(rd_mol)} {get_spin_multiplicity(rd_mol)}'
        for atom in resid.atoms:
            coords += f'\n{atom.element} {atom.position[0]} {atom.position[1]} {atom.position[2]}'
        return coords

    def calc_SAPT(self, input: str, filename: str) -> Dict[str, float]:
        """Runs SAPT on the molecules given in the input string. If `save_psi4_output` is set to true the output will
        be saved as the given filename.
            
            Results are returned in a dictionary with the SAPT energy broken down by type with the following keys.
            
            1. SAPT TOTAL ENERGY
            2. SAPT ELST ENERGY
            3. SAPT EXCH ENERGY
            4. SAPT IND ENERGY
            5. SAPT DISP ENERGY"""
        dimer = psi4.geometry(input)
        psi4.set_options(self._settings)
        psi4.set_memory(self._mem)
        psi4.set_num_threads(self._cfg.system_limits.ncpus)

        if self._save_psi_out:
            psi4.set_output_file(filename)  # Saves output file

        # Calculating SAPT
        psi4.energy(f'{self._method}/{self._basis}', molecule=dimer)

        result: Dict[str, float] = {
            'SAPT TOTAL ENERGY': 0.0,
            'SAPT ELST ENERGY': 0.0,
            'SAPT EXCH ENERGY': 0.0,
            'SAPT IND ENERGY': 0.0,
            'SAPT DISP ENERGY': 0.0
        }

        # Getting results
        result['SAPT TOTAL ENERGY'] = psi4.variable('SAPT TOTAL ENERGY') * self._mht_to_kcalmol
        result['SAPT ELST ENERGY'] = psi4.variable('SAPT ELST ENERGY') * self._mht_to_kcalmol
        result['SAPT EXCH ENERGY'] = psi4.variable('SAPT EXCH ENERGY') * self._mht_to_kcalmol
        result['SAPT IND ENERGY'] = psi4.variable('SAPT IND ENERGY') * self._mht_to_kcalmol
        result['SAPT DISP ENERGY'] = psi4.variable('SAPT DISP ENERGY') * self._mht_to_kcalmol

        return result


class TrajectorySAPT(SAPT, AnalysisBase):
    """Handles iterating over MD trajectory frames,
    setting up SAPT calculations, and processing results.

    Results are stored in a Pandas :class:`DataFrame` following the
    `"tidy dataframe" <https://cran.r-project.org/web/packages/tidyr/vignettes/tidy-data.html>`_
    convention.
    """

    _unv: mda.Universe
    _sel: Dict[int, mda.AtomGroup]
    _sel_pairs: List[Tuple[int, int]]
    results: pd.DataFrame

    def __init__(self, config: Config, optimizer: Optimizer, **universe_kwargs) -> None:
        """Sets up Trajectory and residue selections.

        :Arguments:
            *config*
                :class:`mdsapt.reader.InputReader containing data for running calculations
            *optimizer*
                :class:`mdsapt.optimizer.Optimizer` for preparing residues by replacing missing protons
                and providing a balanced spin state.
            *universe_arguments*
                keyword arguments for loading the trajectory into a MDAnalysis :class:`Universe <MDAnalysis.core.groups.universe.Universe>`
        """
        try:
            # Ensuring config type is correct
            if not isinstance(config.analysis, TrajectoryAnalysisConfig):
                raise ValidationError("config.analysis.type is not trajectory")
        except ValidationError as err:
            logger.exception(err)
            raise err
        self._unv = config.analysis.get_universe(**universe_kwargs)
        elements = guess_types(self._unv.atoms.names)
        self._unv.add_TopologyAttr('elements', elements)
        ag_sel: Set[int] = config.analysis.get_selections()
        self._sel = {x: self._unv.select_atoms(f'resid {x} and not (name OH2 or name H1 or name H2)')
                     for x in ag_sel}
        self._sel_pairs = config.analysis.pairs
        SAPT.__init__(self, config, optimizer)
        AnalysisBase.__init__(self, self._unv.trajectory)

    def _prepare(self) -> None:
        self._col = ['residues', 'time', 'total', 'electrostatic',
                     'exchange', 'induction', 'dispersion']
        self.results = pd.DataFrame(columns=self._col)
        self._res_dict = {x: [] for x in self._col}

    def _single_frame(self) -> None:
        xyz_dict = {k: self.get_psi_mol(k, self._sel[k]) for k in self._sel.keys()}
        for pair in self._sel_pairs:
            coords = xyz_dict[pair[0]] + '\n--\n' + xyz_dict[pair[1]] + '\nunits angstrom'

            logger.info(f'Starting SAPT for {pair}')

            sapt: Dict[str, float] = self.calc_SAPT(coords, f'sapt_{pair[0]}-{pair[1]}_{self._ts.time}.out')
            result = [f'{pair[0]}-{pair[1]}', self._ts.time] + [sapt[x] for x in
                                                                ['SAPT TOTAL ENERGY', 'SAPT ELST ENERGY',
                                                                 'SAPT EXCH ENERGY', 'SAPT IND ENERGY',
                                                                 'SAPT DISP ENERGY']]

            for r in range(len(result)):
                self._res_dict[self._col[r]].append(result[r])

    def _conclude(self) -> None:
        for k in self._col:
            self.results[k] = self._res_dict[k]


class DockingSAPT(SAPT):
    """"""

    _ens: Ensemble
    _sel: Dict[int, EnsembleAtomGroup]

    def __init__(self, config: Config, optimizer: Optimizer) -> None:
        try:
            # Ensuring config type is correct
            if not isinstance(config.analysis, DockingAnalysisConfig):
                raise ValidationError("config.analysis.type is not docking")
        except ValidationError as err:
            logger.exception(err)
            raise err

        if config.analysis.mode == DockingStructureMode.MergedLigand:
            self._ens = Ensemble(systems_dir=config.analysis.combined_topologies)
        elif config.analysis.mode == DockingStructureMode.SeparateLigand:
            self._ens = Ensemble(protein_dir=config.analysis.protein,
                                 ligands_dir=config.analysis.ligands)
        else:
            err = ValidationError("Must specify either 'protein-ligand' or 'separate-ligand' mode in config")
            logger.exception(err)
            raise err

        self._sel = {k: self._ens.select_atoms(f'resid {k} and not (name OH2 or name H1 or name H2') for k in
                     self._cfg.ag_sel}
        super(DockingSAPT, self).__init__(config, optimizer)

    def _prepare(self) -> None:
        self._col = ['structure', 'time', 'total', 'electrostatic',
                     'exchange', 'induction', 'dispersion']
        self.results = pd.DataFrame(columns=self._col)
        self._res_dict = {x: [] for x in self._col}

    def _single_system(self) -> None:
        xyz_dict = {k: self.get_psi_mol(self._sel[k][self._key]) for k in self._sel.keys()}
        for pair in self._sel_pairs:
            coords = xyz_dict[pair[0]] + '\n--\n' + xyz_dict[pair[1]] + '\nunits angstrom'

            logger.info(f'Starting SAPT for {pair}')

            sapt: Dict[str, float] = self.calc_SAPT(coords, f'sapt_{pair[0]}-{pair[1]}_{self._key}.out')
            result = [f'{pair[0]}-{pair[1]}', self._key] + [sapt[x] for x in ['SAPT TOTAL ENERGY', 'SAPT ELST ENERGY',
                                                                              'SAPT EXCH ENERGY', 'SAPT IND ENERGY',
                                                                              'SAPT DISP ENERGY']]

            for r in range(len(result)):
                self._res_dict[self._col[r]].append(result[r])

    def _conclude(self) -> None:
        for k in self._col:
            self.results[k] = self._res_dict[k]

    def run(self):
        """Runs _single_universe on each system and _single_frame
        on each frame in the system.
        First iterates through keys of ensemble, then runs _setup_system
        which defines the system and trajectory. Then iterates over
        trajectory frames.
        """
        logger.info("Setting up systems")
        for self._key in ProgressBar(self._ens.keys(), verbose=True):
            self._prepare()
            self._single_system()
            logger.info("Moving to next universe")
        logger.info("Finishing up")
        self._conclude()
        return self
