# -*- coding: utf-8 -*-
import os
import subprocess
import sys
import time
from importlib import import_module

import pytest

from nameko_grpc.client import Client

from helpers import Config, FifoPipe, GrpcError, receive, send


def pytest_addoption(parser):

    parser.addoption(
        "--client",
        action="store",
        type="choice",
        choices=["nameko", "grpc", "all"],
        dest="client",
        default="all",
        help="Use this client type",
    )

    parser.addoption(
        "--server",
        action="store",
        type="choice",
        choices=["nameko", "grpc", "all"],
        dest="server",
        default="all",
        help="Use this server type",
    )


@pytest.fixture(scope="session")
def spec_dir(tmpdir_factory):
    master = os.path.join(os.path.dirname(__file__), "spec")
    temp = tmpdir_factory.mktemp("spec")
    for filename in os.listdir(master):
        path = os.path.join(master, filename)
        if os.path.isfile(path):
            copy = temp.join(filename)
            with open(path) as file:
                copy.write(file.read())
    return temp


@pytest.fixture
def compile_proto():
    def codegen(service_name, spec_path):
        proto_path = os.path.join(spec_path, "{}.proto".format(service_name))
        proto_last_modified = os.path.getmtime(proto_path)

        for generated_file in (
            "{}_pb2.py".format(service_name),
            "{}_pb2_grpc.py".format(service_name),
        ):
            generated_path = os.path.join(spec_path, generated_file)
            if (
                not os.path.exists(generated_path)
                or os.path.getmtime(generated_path) < proto_last_modified
            ):
                protoc_args = [
                    "-I{}".format(spec_path),
                    "--python_out",
                    spec_path,
                    "--grpc_python_out",
                    spec_path,
                    proto_path,
                ]
                # protoc.main is confused by absolute paths, so use subprocess instead
                python_args = ["python", "-m", "grpc_tools.protoc"] + protoc_args
                subprocess.call(python_args)

        if spec_path not in sys.path:
            sys.path.append(spec_path)

        protobufs = import_module("{}_pb2".format(service_name))
        stubs = import_module("{}_pb2_grpc".format(service_name))

        return protobufs, stubs

    return codegen


# XXX do we actually want these?
@pytest.fixture
def compile_protobufs(compile_proto, spec_dir):
    def make(service_name):
        protobufs, _ = compile_proto(service_name, spec_dir.strpath)
        return protobufs

    return make


# XXX do we actually want these?
@pytest.fixture
def compile_stubs(compile_proto, spec_dir):
    def make(service_name):
        _, stubs = compile_proto(service_name, spec_dir.strpath)
        return stubs

    return make


@pytest.fixture
def make_fifo(tmpdir):

    fifos = []

    def make():
        fifo = FifoPipe.new(tmpdir.strpath)
        fifos.append(fifo)
        fifo.open()
        return fifo

    yield make

    for fifo in fifos:
        fifo.close()


@pytest.fixture
def spawn_process():

    procs = []

    def spawn(*args):
        popen_args = [sys.executable]
        popen_args.extend(args)
        procs.append(subprocess.Popen(popen_args))

    yield spawn

    for proc in procs:
        proc.terminate()


@pytest.fixture
def start_grpc_server(compile_proto, spawn_process, spec_dir):

    server_script = os.path.join(os.path.dirname(__file__), "grpc_indirect_server.py")

    def make(service_name, spec_path=None):
        if spec_path is None:
            spec_path = spec_dir.strpath
        compile_proto(service_name, spec_path)

        service_path = "{}_grpc.{}".format(service_name, service_name)
        spawn_process(server_script, service_path, spec_path)
        # wait until server has started
        time.sleep(0.5)

    yield make


@pytest.fixture
def start_grpc_client(compile_proto, tmpdir, make_fifo, spawn_process, spec_dir):

    client_script = os.path.join(os.path.dirname(__file__), "grpc_indirect_client.py")

    client_fifos = []

    class Result:
        def __init__(self, fifo):
            self.fifo = fifo

        def result(self):
            res = receive(self.fifo)
            if isinstance(res, GrpcError):
                raise res
            return res

    class Method:
        def __init__(self, fifo, name):
            self.fifo = fifo
            self.name = name

        def __call__(self, request):
            return self.future(request).result()

        def future(self, request):
            in_fifo = make_fifo()
            out_fifo = make_fifo()
            send(self.fifo, Config(self.name, in_fifo.path, out_fifo.path))
            send(in_fifo, request)
            return Result(out_fifo)

    class Client:
        def __init__(self, fifo):
            self.fifo = fifo

        def __getattr__(self, name):
            return Method(self.fifo, name)

    def make(service_name, spec_path=None):
        if spec_path is None:
            spec_path = spec_dir.strpath

        compile_proto(service_name, spec_path)

        client_fifo = make_fifo()
        client_fifos.append(client_fifo)

        spawn_process(client_script, spec_path, service_name, client_fifo.path)

        return Client(client_fifo)

    yield make

    for fifo in client_fifos:
        send(fifo, None)


@pytest.fixture
def start_nameko_server(compile_proto, spec_dir, container_factory):
    def make(service_name, spec_path=None):
        if spec_path is None:
            spec_path = spec_dir.strpath

        sys.path.append(spec_path)  # XXX  remove this again?

        compile_proto(service_name, spec_path)
        service_module = import_module("{}_nameko".format(service_name))
        service_cls = getattr(service_module, service_name)

        container = container_factory(service_cls, {})
        container.start()

        return container

    yield make


@pytest.fixture
def start_nameko_client(compile_proto, spec_dir):

    clients = []

    def make(service_name, spec_path=None):
        if spec_path is None:
            spec_path = spec_dir.strpath
        _, stubs = compile_proto(service_name, spec_path)

        stub_cls = getattr(stubs, "{}Stub".format(service_name))
        client = Client("//127.0.0.1", stub_cls)
        clients.append(client)
        return client.start()

    yield make

    for client in clients:
        client.stop()
